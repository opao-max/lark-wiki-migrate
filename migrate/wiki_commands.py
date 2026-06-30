"""wiki-migrate 子命令实现（scan / run / verify / precheck / migrate-one）。

入口脚本 `wiki-migrate.py` 只负责注册信号与调用 `main()`，具体逻辑集中在此模块，
便于阅读与展示。
"""

import json
import time
import os
import argparse
from datetime import datetime

from common import (
    WORK_DIR, PROGRESS_INTERVAL,
    log, lark_api, run_lark_json,
    load_state, save_state, set_node, find_node,
    MigrationError, DocDeletedError, LarkPermissionError,
)
from block_api import read_docx_blocks
from strategies import (
    EXPORT_IMPORT_TYPES, PLACEHOLDER_TYPES,
    migrate_node, migrate_shortcut,
)

# ============================================================
# State helpers
# ============================================================

def _build_state_meta(source_space_id, target_space_id, source_profile, target_profile, target_root=""):
    """构建 state.meta。scan 与 migrate-one 都复用这一份结构。"""
    return {
        "source_space_id": source_space_id,
        "target_space_id": target_space_id,
        "source_profile": source_profile,
        "target_profile": target_profile,
        "target_root_node_token": target_root,
        "started_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
    }


def _make_state_node(raw_node, depth=0):
    """将扫描结果/API 节点信息规范化为 state['nodes'] 中的一条记录。"""
    return {
        "node_token":        raw_node.get("node_token", ""),
        "obj_type":          raw_node.get("obj_type", ""),
        "obj_token":         raw_node.get("obj_token", ""),
        "title":             raw_node.get("title", ""),
        "parent_node_token": raw_node.get("parent_node_token", ""),
        "node_type":         raw_node.get("node_type", "origin"),
        "origin_node_token": raw_node.get("origin_node_token") or "",
        "origin_space_id":   raw_node.get("origin_space_id") or "",
        "depth":             raw_node.get("_depth", depth),
        "status":            "pending",
        "target_obj_token":  None,
        "target_node_token": None,
        "error":             None,
        "retries":           0,
    }


def _build_state(source_space_id, target_space_id, source_profile, target_profile, target_root=""):
    """构建空 state。"""
    return {
        "meta": _build_state_meta(
            source_space_id=source_space_id,
            target_space_id=target_space_id,
            source_profile=source_profile,
            target_profile=target_profile,
            target_root=target_root,
        ),
        "node_mapping": {},
        "obj_mapping": {},
        "nodes": [],
        "stats": {},
    }


def _mark_done(state, node, target_obj_token, target_node_token):
    """将节点标记为 done，并同步更新映射表。"""
    node_token = node["node_token"]
    set_node(
        state,
        node_token,
        status="done",
        target_obj_token=target_obj_token,
        target_node_token=target_node_token,
        error=None,
    )
    state["node_mapping"][node_token] = target_node_token
    if target_obj_token:
        state["obj_mapping"][node["obj_token"]] = target_obj_token


def _mark_skipped(state, node, error):
    set_node(state, node["node_token"], status="skipped", error=str(error)[:500])


def _mark_failed(state, node, error):
    set_node(
        state,
        node["node_token"],
        status="failed",
        error=str(error)[:500],
        retries=node.get("retries", 0) + 1,
    )


# ============================================================
# Scan command
# ============================================================

def _recursive_scan(space_id, parent_tk, profile, depth=0):
    """递归列举知识库中所有节点（DFS，最大深度 20 层防止死循环）"""
    if depth > 20:
        return []

    params = {"space_id": space_id}
    if parent_tk:
        params["parent_node_token"] = parent_tk

    try:
        data = run_lark_json(
            ["wiki", "nodes", "list", "--page-all",
             "--params", json.dumps(params, ensure_ascii=False)],
            profile=profile, timeout=120)
    except Exception as e:
        log(f"  ERROR listing {parent_tk}: {e}")
        return []

    if not data:
        return []

    items = data.get("data", {}).get("items", [])
    result = []
    for item in items:
        item['_depth'] = depth
        result.append(item)
        if item.get("has_child"):
            time.sleep(0.3)
            result.extend(_recursive_scan(space_id, item["node_token"], profile, depth + 1))
    return result


def _compute_depths(nodes):
    """根据 parent-child 关系计算每个节点的深度（用于从文件导入时，API 扫描时已有 _depth）"""
    token_to_node = {n['node_token']: n for n in nodes}
    cache = {}

    def depth_of(nt):
        if nt in cache:
            return cache[nt]
        n = token_to_node.get(nt)
        if not n:
            cache[nt] = 0
            return 0
        p = n.get('parent_node_token', '')
        if not p or p not in token_to_node:
            cache[nt] = 0
        else:
            cache[nt] = depth_of(p) + 1
        return cache[nt]

    for n in nodes:
        n['_depth'] = depth_of(n['node_token'])


def _load_scan_nodes(args):
    """加载 scan 阶段的原始节点列表：要么递归扫描，要么从文件导入。"""
    if args.from_file:
        log(f"Loading nodes from {args.from_file}...")
        with open(args.from_file, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        if isinstance(raw, list):
            raw_nodes = raw
        elif isinstance(raw, dict) and 'nodes' in raw:
            raw_nodes = raw['nodes']
        elif isinstance(raw, dict) and 'data' in raw:
            raw_nodes = raw['data'].get('items', [])
        else:
            raise MigrationError("unrecognized file format")
        _compute_depths(raw_nodes)
        return raw_nodes

    log(f"Scanning source wiki (space={args.source_space}, profile={args.source_profile})...")
    return _recursive_scan(args.source_space, None, args.source_profile)


def _enrich_shortcut_nodes(raw_nodes, profile):
    """补齐 shortcut 的 origin 信息。wiki nodes list 不返回这部分字段。"""
    shortcut_nodes = [n for n in raw_nodes if n.get('node_type') == 'shortcut']
    if not shortcut_nodes:
        return

    log(f"Enriching {len(shortcut_nodes)} shortcut(s) with origin info...")
    for shortcut in shortcut_nodes:
        nt = shortcut['node_token']
        try:
            r = run_lark_json(
                ["wiki", "spaces", "get_node",
                 "--params", json.dumps({"token": nt})],
                profile=profile, timeout=30)
            if r and r.get("code") == 0:
                nd = r.get("data", {}).get("node", {})
                shortcut['origin_node_token'] = nd.get('origin_node_token', '')
                shortcut['origin_space_id'] = nd.get('origin_space_id', '')
                log(f"  {shortcut['title'][:40]}: origin_space={shortcut['origin_space_id']}")
            time.sleep(0.3)
        except Exception as e:
            log(f"  WARN: failed to get origin for {nt}: {e}")


def _log_scan_summary(raw_nodes):
    """打印 scan 结果统计。"""
    types = {}
    for n in raw_nodes:
        t = n.get('obj_type', '?')
        types[t] = types.get(t, 0) + 1
    max_depth = max(n.get('_depth', 0) for n in raw_nodes)

    log(f"Found {len(raw_nodes)} nodes, max depth {max_depth}:")
    for t in sorted(types, key=types.get, reverse=True):
        extra = ""
        if t in PLACEHOLDER_TYPES:
            extra = " (placeholder)"
        elif t == 'shortcut':
            extra = " (deferred)"
        log(f"  {t}: {types[t]}{extra}")


def cmd_scan(args):
    sp = args.source_profile
    tp = args.target_profile
    ssi = args.source_space
    tsi = args.target_space or ""

    os.makedirs(WORK_DIR, exist_ok=True)

    try:
        raw_nodes = _load_scan_nodes(args)
    except MigrationError as e:
        log(f"ERROR: {e}")
        return 1

    if not raw_nodes:
        log("ERROR: no nodes found!")
        return 1

    _enrich_shortcut_nodes(raw_nodes, sp)
    _log_scan_summary(raw_nodes)

    target_root = getattr(args, 'target_root', '') or ''
    state = _build_state(ssi, tsi, sp, tp, target_root=target_root)
    state['nodes'] = [_make_state_node(n) for n in raw_nodes]

    save_state(args.state, state)
    log(f"State saved to {args.state} ({len(raw_nodes)} nodes)")
    return 0

# ============================================================
# 单文档迁移
# ============================================================

def _run_single_node(args, state):
    """迁移 state 中指定的单个节点。

    支持 origin 和 shortcut 类型。
    父节点未迁移时放到 target_root 下（而非报错），方便测试。
    """
    meta = state['meta']
    sp = meta['source_profile']
    tp = meta['target_profile']
    tsi = meta['target_space_id']
    node_token = args.node_token

    node = find_node(state, node_token)
    if not node:
        log(f"ERROR: node_token {node_token} not found in state")
        return 1

    if node['status'] == 'done':
        log(f"Node already done: [{node['obj_type']}] {node['title'][:60]}")
        log(f"  target_node_token={node.get('target_node_token')}")
        log(f"  target_obj_token={node.get('target_obj_token')}")
        return 0

    title = node['title'][:60] or "(untitled)"
    log(f"Single node migration: [{node['obj_type']}] {title}")
    log(f"  Source: {sp} ({meta['source_space_id']}) → Target: {tp} ({tsi})")

    try:
        if node['node_type'] == 'shortcut':
            tgt_obj, tgt_node = migrate_shortcut(node, state)
        else:
            tgt_obj, tgt_node = migrate_node(node, state)

        if not tgt_node:
            raise MigrationError("node_token is empty after migration!")

        _mark_done(state, node, tgt_obj, tgt_node)
        save_state(args.state, state)
        log(f"  DONE → node_token={tgt_node}, obj_token={tgt_obj}")
        return 0

    except DocDeletedError as e:
        log(f"  SKIP (deleted): {e}")
        _mark_skipped(state, node, e)
        save_state(args.state, state)
        return 0

    except Exception as e:
        log(f"  FAILED: {type(e).__name__}: {e}")
        _mark_failed(state, node, e)
        save_state(args.state, state)
        return 1

# ============================================================
# Run / Resume command
# ============================================================

def cmd_run(args):
    state = load_state(args.state)
    meta = state['meta']
    sp = meta['source_profile']
    tp = meta['target_profile']
    tsi = meta['target_space_id']

    if not tsi:
        log("ERROR: target_space_id not set! Use --target-space in scan.")
        return 1

    os.makedirs(WORK_DIR, exist_ok=True)

    # ---- 单文档迁移模式 ----
    if getattr(args, 'node_token', None):
        return _run_single_node(args, state)

    log(f"Migration start — Source: {sp} ({meta['source_space_id']}) → Target: {tp} ({tsi})")

    os.makedirs(WORK_DIR, exist_ok=True)

    nodes = state['nodes']

    # 分两阶段迁移：先迁移所有源文档（origin），再处理快捷方式（shortcut）
    # 因为快捷方式需要指向目标中已存在的源文档
    origins = [n for n in nodes if n['node_type'] != 'shortcut']
    shortcuts = [n for n in nodes if n['node_type'] == 'shortcut']

    # 按深度排序确保父节点先于子节点迁移（子节点需要知道父节点的目标 token）
    origins.sort(key=lambda n: n.get('depth', 0))

    # ---- Phase 1: Migrate origin nodes ----
    done_cnt = sum(1 for n in origins if n['status'] in ('done', 'skipped'))
    total_origins = len(origins)
    log(f"Origin nodes: {total_origins} total, {done_cnt} already done/skipped")

    for i, node in enumerate(origins):
        if node['status'] in ('done', 'skipped'):
            continue

        nt = node['node_token']
        obj_type = node['obj_type']
        title = node['title'][:50] or "(untitled)"
        idx = done_cnt + 1

        # Skip if parent failed or not ready
        psrc = node.get('parent_node_token', '')
        if psrc:
            pnode = find_node(state, psrc)
            if pnode and pnode['status'] == 'failed':
                log(f"[{idx}/{total_origins}] DEFER {obj_type}: {title} (parent failed, will retry later)")
                continue
            # Parent exists in state but not yet migrated
            if pnode and pnode['status'] not in ('done', 'skipped'):
                # Check if parent was migrated in a previous batch (has mapping)
                if psrc not in state['node_mapping']:
                    log(f"[{idx}/{total_origins}] DEFER {obj_type}: {title} (parent not ready)")
                    continue

        log(f"[{idx}/{total_origins}] {obj_type}: {title}")

        try:
            tgt_obj, tgt_node = migrate_node(node, state)

            # CRITICAL: node_token must not be empty
            if not tgt_node:
                raise MigrationError("node_token is empty after migration!")

            _mark_done(state, node, tgt_obj, tgt_node)
            log(f"  DONE → node_token={tgt_node}")

        except DocDeletedError as e:
            log(f"  SKIP (deleted): {e}")
            _mark_skipped(state, node, e)

        except LarkPermissionError as e:
            log(f"  FAILED (permission): {e}")
            _mark_failed(state, node, e)

        except Exception as e:
            log(f"  FAILED: {type(e).__name__}: {e}")
            _mark_failed(state, node, e)

        save_state(args.state, state)
        done_cnt += 1

        # Progress + integrity check
        if done_cnt % PROGRESS_INTERVAL == 0:
            log(f"--- Progress: {done_cnt}/{total_origins} origins ---")
            _check_integrity(state)

        time.sleep(0.5)

    # ---- 阶段 1b: 重试被推迟的节点 ----
    # 有些节点在第一遍时因为父节点还没迁移成功而被跳过，现在重试
    deferred = [n for n in origins if n['status'] == 'pending']
    if deferred:
        log(f"\nRetrying {len(deferred)} deferred nodes...")
        for node in deferred:
            nt = node['node_token']
            obj_type = node['obj_type']
            title = node['title'][:50] or "(untitled)"

            psrc = node.get('parent_node_token', '')
            if psrc:
                pnode = find_node(state, psrc)
                if pnode and pnode['status'] not in ('done',):
                    set_node(state, nt, status='skipped', error=f"parent {psrc} status={pnode['status'] if pnode else 'missing'}")
                    save_state(args.state, state)
                    continue

            log(f"[retry] {obj_type}: {title}")
            try:
                tgt_obj, tgt_node = migrate_node(node, state)
                if not tgt_node:
                    raise MigrationError("node_token is empty after migration!")
                _mark_done(state, node, tgt_obj, tgt_node)
                log(f"  DONE → node_token={tgt_node}")
            except DocDeletedError as e:
                _mark_skipped(state, node, e)
            except Exception as e:
                log(f"  FAILED: {type(e).__name__}: {e}")
                _mark_failed(state, node, e)
            save_state(args.state, state)
            time.sleep(0.5)

    # ---- Phase 2: Migrate shortcuts ----
    log(f"\nShortcuts: {len(shortcuts)} total")
    for node in shortcuts:
        if node['status'] in ('done', 'skipped'):
            continue

        nt = node['node_token']
        title = node['title'][:50] or "(untitled)"

        log(f"[shortcut] {title}")

        try:
            tgt_obj, tgt_node = migrate_shortcut(node, state)
            _mark_done(state, node, tgt_obj, tgt_node)
            log(f"  DONE → {tgt_node}")

        except DocDeletedError as e:
            log(f"  SKIP (broken): {e}")
            _mark_skipped(state, node, e)

        except Exception as e:
            log(f"  FAILED: {e}")
            _mark_failed(state, node, e)

        save_state(args.state, state)
        time.sleep(0.5)

    # ---- Final report ----
    save_state(args.state, state)  # ensure final stats
    s = state['stats']
    log(f"\n{'='*50}")
    log(f"Migration complete!")
    log(f"  Total:   {s['total']}")
    log(f"  Done:    {s['done']}")
    log(f"  Failed:  {s['failed']}")
    log(f"  Skipped: {s['skipped']}")
    log(f"  Pending: {s['pending']}")

    if s['failed'] > 0:
        log(f"\nFailed nodes:")
        for n in state['nodes']:
            if n['status'] == 'failed':
                log(f"  [{n['obj_type']}] {n['title'][:60]}")
                log(f"    error: {n.get('error', '')[:200]}")
                log(f"    token: {n['node_token']}")

    return 0 if s['failed'] == 0 else 1


def _check_integrity(state):
    """完整性抽检：确保标记为 done 的节点都有 target_node_token"""
    missing = 0
    for n in state['nodes']:
        if n['status'] == 'done' and not n.get('target_node_token'):
            missing += 1
    if missing:
        log(f"  WARNING: {missing} done nodes have no target_node_token!")

# ============================================================
# Verify command
# ============================================================

def cmd_verify(args):
    state = load_state(args.state)
    meta = state['meta']
    tp = meta['target_profile']
    tsi = meta['target_space_id']

    log("Verifying migration...")

    s = state['stats']
    log(f"  State: total={s['total']}, done={s['done']}, failed={s['failed']}, skipped={s['skipped']}")

    # Check mappings
    mapping = state.get('node_mapping', {})
    done_nodes = [n for n in state['nodes'] if n['status'] == 'done']
    log(f"  Mapping entries: {len(mapping)}")
    log(f"  Done nodes: {len(done_nodes)}")

    missing = [n for n in done_nodes if not n.get('target_node_token')]
    if missing:
        log(f"  WARNING: {len(missing)} done nodes without target_node_token!")

    # Type breakdown
    type_stats = {}
    for n in state['nodes']:
        t = n['obj_type']
        st = n['status']
        type_stats.setdefault(t, {}).setdefault(st, 0)
        type_stats[t][st] = type_stats[t].get(st, 0) + 1

    log(f"\n  Type breakdown:")
    for t in sorted(type_stats):
        parts = [f"{st}={c}" for st, c in sorted(type_stats[t].items())]
        log(f"    {t}: {', '.join(parts)}")

    # Sample verify: check target nodes exist (default 10, --all for all)
    import random
    verify_all = getattr(args, 'verify_all', False)
    if verify_all:
        sample_size = len(done_nodes)
        samples = done_nodes
        log(f"\n  Full verification: checking all {sample_size} done nodes...")
    else:
        sample_size = min(10, len(done_nodes))
        samples = random.sample(done_nodes, sample_size) if sample_size > 0 else []
        log(f"\n  Sample verification: checking {sample_size} random nodes...")
    if sample_size > 0:
        ok = 0
        for n in samples:
            tgt = n.get('target_node_token')
            if not tgt:
                continue
            try:
                r = lark_api("GET", f"/open-apis/wiki/v2/spaces/get_node",
                             params={"token": tgt},
                             profile=tp, timeout=10)
                if r and r.get("code") == 0:
                    ok += 1
            except Exception:
                pass
        log(f"\n  Sample check: {ok}/{sample_size} target nodes verified OK")

    # List failed
    failed = [n for n in state['nodes'] if n['status'] == 'failed']
    if failed:
        log(f"\n  Failed nodes ({len(failed)}):")
        for n in failed[:20]:
            log(f"    [{n['obj_type']}] {n['title'][:50]} — {n.get('error','')[:100]}")

    return 0

# ============================================================
# Precheck command
# ============================================================

def cmd_precheck(args):
    """预检命令：扫描所有 docx 的 blocks，生成迁移能力分析报告。

    统计各种 block 类型的数量，帮助评估哪些内容能自动迁移、
    哪些会降级、哪些需要手动处理。
    """
    state = load_state(args.state)
    sp = state['meta']['source_profile']

    docx_nodes = [n for n in state['nodes']
                  if n['obj_type'] == 'docx' and n.get('node_type') != 'shortcut']

    log(f"Precheck: scanning {len(docx_nodes)} docx nodes for content analysis...")

    from collections import Counter
    stats = {
        "total_blocks": 0,
        "block_types": Counter(),
        "images": 0,
        "embedded_sheets": 0,
        "embedded_files": 0,
        "boards": 0,
        "mention_users": 0,
        "mention_docs": 0,
        "internal_links": 0,
        "errors": 0,
    }

    for idx, node in enumerate(docx_nodes):
        if (idx + 1) % 100 == 0:
            log(f"  [{idx+1}/{len(docx_nodes)}]...")

        try:
            blocks = read_docx_blocks(node['obj_token'], sp)
        except Exception:
            stats["errors"] += 1
            continue

        stats["total_blocks"] += len(blocks)
        for block in blocks:
            bt = block.get("block_type", -1)
            stats["block_types"][bt] += 1

            if block.get("image", {}).get("token"):
                stats["images"] += 1
            if bt == 30:
                stats["embedded_sheets"] += 1
            if bt == 23:
                stats["embedded_files"] += 1
            if bt == 43:
                stats["boards"] += 1

            for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                          'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                          'bullet', 'ordered', 'quote', 'todo', 'callout'):
                content = block.get(field)
                if not content or not isinstance(content, dict):
                    continue
                for elem in content.get('elements', []):
                    if elem.get('mention_user'):
                        stats["mention_users"] += 1
                    if elem.get('mention_doc'):
                        stats["mention_docs"] += 1
                    tr = elem.get('text_run', {})
                    url = tr.get('text_element_style', {}).get('link', {}).get('url', '')
                    if url and ('feishu.cn' in url or 'larksuite.com' in url):
                        stats["internal_links"] += 1

        time.sleep(0.3)

    # Type breakdown
    type_counts = Counter(n['obj_type'] for n in state['nodes'])

    log(f"\n{'='*60}")
    log(f"预检报告 — 迁移能力分析")
    log(f"{'='*60}")
    log(f"总节点: {len(state['nodes'])}")
    for t, c in type_counts.most_common():
        log(f"  {t}: {c}")

    log(f"\nDOCX 内容分析 ({len(docx_nodes)} 个文档, {stats['total_blocks']} blocks):")
    log(f"  可完整迁移:")
    log(f"     图片: {stats['images']}")
    log(f"     原生表格(31): {stats['block_types'].get(31, 0)}")
    log(f"     Grid分栏(24): {stats['block_types'].get(24, 0)}")
    log(f"     Callout(19): {stats['block_types'].get(19, 0)}")
    log(f"     引用容器(34): {stats['block_types'].get(34, 0)}")

    log(f"  降级迁移（自动处理，内容会保留但格式可能变化）:")
    log(f"     嵌入文件附件(23): {stats['embedded_files']} -> 下载上传替换token")
    log(f"     嵌入电子表格(30): {stats['embedded_sheets']} -> 尝试单独迁移sheet")
    log(f"     @文档提及: {stats['mention_docs']} -> 转为超链接，fix-links.py修复")
    log(f"     @用户提及: {stats['mention_users']} -> 转为文本，fix-mentions.py修复")
    log(f"     飞书内链: {stats['internal_links']} -> fix-links.py修复")

    log(f"  无法自动迁移（生成占位提示）:")
    log(f"     画板/白板(43): {stats['boards']}")
    unknown = sum(c for bt, c in stats['block_types'].items()
                  if bt not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 13, 14, 17,
                                19, 22, 23, 24, 25, 27, 30, 31, 32, 33, 34, 43))
    log(f"     未知block类型: {unknown}")

    log(f"\n其他类型:")
    log(f"  doc(旧格式): {type_counts.get('doc', 0)} -> export为docx导入")
    log(f"  sheet: {type_counts.get('sheet', 0)} -> export为xlsx导入（丢失公式）")
    log(f"  bitable: {type_counts.get('bitable', 0)} -> export为xlsx导入（丢失自动化）")
    log(f"  mindnote: {type_counts.get('mindnote', 0)} -> 浏览器自动化（丢失图片/链接/样式）")
    log(f"  file: {type_counts.get('file', 0)} -> 下载上传")
    log(f"  slides: {type_counts.get('slides', 0)} -> 占位文档（无API）")
    log(f"  shortcut: {type_counts.get('shortcut', 0)}")

    if stats['errors'] > 0:
        log(f"\n  扫描错误: {stats['errors']} 个文档无法读取")

    # Save report
    report_path = args.state.replace('.json', '-precheck.json')
    report = {
        "scan_time": datetime.now().isoformat(),
        "type_counts": dict(type_counts),
        "docx_stats": {k: v if not isinstance(v, (set, Counter)) else dict(v) for k, v in stats.items()},
    }
    tmp = report_path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    os.replace(tmp, report_path)
    log(f"\n报告已保存: {report_path}")

    return 0

# ============================================================
# report：迁移报告
# ============================================================

# 需手动处理的占位符模式
_PROBLEM_PLACEHOLDERS = [
    (r'\[画板: [^\]]*\] \(画板无法跨组织', '画板(无缩略图)'),
    (r'\[嵌入多维表格: [^\]]*\]', '嵌入多维表格'),
    (r'\[嵌入思维导图: [^\]]*\]', '嵌入思维导图'),
    (r'\[嵌入文件: [^\]]*\] \(迁移失败\)', '嵌入文件(失败)'),
    (r'\[不支持的内容类型: [^\]]*\]', '不支持的内容'),
    (r'\[插件: [^\]]*\]', '插件'),
    (r'\[任务: [^\]]*\]', '任务'),
    (r'\[多维表格视图: [^\]]*\]', '多维表格视图'),
    (r'\[此内容已失效\]', '已失效内容'),
    (r'\[画板/图表: [^\]]*\]', '画板/图表'),
]


def _scan_placeholders(blocks):
    """扫描 blocks 中的占位符，返回 {类型: 数量} 字典。"""
    import re
    counts = {}
    for b in blocks:
        for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                      'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                      'bullet', 'ordered', 'quote', 'todo', 'callout'):
            content = b.get(field)
            if not content or not isinstance(content, dict):
                continue
            text = "".join(
                e.get("text_run", {}).get("content", "")
                for e in content.get("elements", []))
            for pattern, label in _PROBLEM_PLACEHOLDERS:
                for _ in re.finditer(pattern, text):
                    counts[label] = counts.get(label, 0) + 1
    return counts


def _make_url(domain, node_token):
    if not domain or not node_token:
        return ""
    return f"https://{domain}/wiki/{node_token}"


def cmd_report(args):
    """生成迁移报告。"""
    state = load_state(args.state)
    meta = state['meta']
    sp = meta['source_profile']
    tp = meta['target_profile']
    src_domain = args.source_domain
    tgt_domain = args.target_domain

    nodes = state.get('nodes', [])
    done_nodes = [n for n in nodes if n['status'] == 'done']
    failed_nodes = [n for n in nodes if n['status'] == 'failed']
    skipped_nodes = [n for n in nodes if n['status'] == 'skipped']

    # 分类 done 节点
    placeholder_docs = []   # mindnote/slides 占位文档
    clean_docs = []         # 完全迁移
    problem_docs = []       # 有占位符

    docx_done = [n for n in done_nodes if n['obj_type'] in ('docx', 'doc')]
    non_docx_done = [n for n in done_nodes if n['obj_type'] not in ('docx', 'doc')]

    # mindnote/slides 单独列出
    for n in done_nodes:
        if n['obj_type'] in ('mindnote', 'slides'):
            placeholder_docs.append(n)
    non_docx_clean = [n for n in non_docx_done if n['obj_type'] not in ('mindnote', 'slides')]

    # 扫描 docx/doc 的占位符
    log(f"Scanning {len(docx_done)} docx/doc documents for placeholders...")
    for idx, n in enumerate(docx_done):
        tgt_obj = n.get('target_obj_token')
        if not tgt_obj:
            problem_docs.append((n, {'无目标token': 1}))
            continue
        if (idx + 1) % 50 == 0:
            log(f"  [{idx+1}/{len(docx_done)}]...")
        try:
            blocks = read_docx_blocks(tgt_obj, tp)
            placeholders = _scan_placeholders(blocks)
            if placeholders:
                problem_docs.append((n, placeholders))
            else:
                clean_docs.append(n)
        except Exception as e:
            problem_docs.append((n, {f'扫描失败: {str(e)[:50]}': 1}))
        time.sleep(0.3)

    # 非 docx 的 done 节点（sheet/bitable/file）算完全迁移
    clean_docs.extend(non_docx_clean)

    # 生成报告
    report_path = args.output or args.state.replace('.json', '-report.md')
    lines = []
    lines.append("# 迁移报告\n")
    lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"源: {sp} → 目标: {tp}\n")

    lines.append("## 总览\n")
    lines.append("| 状态 | 数量 |")
    lines.append("|------|------|")
    lines.append(f"| 完全迁移 | {len(clean_docs)} |")
    lines.append(f"| 有占位符（需检查） | {len(problem_docs)} |")
    lines.append(f"| 迁移失败 | {len(failed_nodes)} |")
    lines.append(f"| 占位文档（mindnote/slides） | {len(placeholder_docs)} |")
    lines.append(f"| 跳过 | {len(skipped_nodes)} |")
    lines.append("")

    # 含占位符的文档
    if problem_docs:
        lines.append("## 含占位符的文档\n")
        lines.append("自动迁移成功，但部分内容因 API 限制降级为占位符。\n")
        lines.append("| 文档 | 类型 | 占位符 | 源文档 | 目标文档 |")
        lines.append("|------|------|--------|--------|---------|")
        for n, placeholders in problem_docs:
            ph_str = ", ".join(f"{k}×{v}" for k, v in placeholders.items())
            src_url = _make_url(src_domain, n.get('node_token', ''))
            tgt_url = _make_url(tgt_domain, n.get('target_node_token', ''))
            src_link = f"[查看]({src_url})" if src_url else "-"
            tgt_link = f"[查看]({tgt_url})" if tgt_url else "-"
            lines.append(f"| {n['title'][:40]} | {n['obj_type']} | {ph_str} | {src_link} | {tgt_link} |")
        lines.append("")

    # 迁移失败
    if failed_nodes:
        lines.append("## 迁移失败\n")
        lines.append("需要手动从源文档导出后上传到目标知识库。\n")
        lines.append("| 文档 | 类型 | 错误原因 | 源文档 |")
        lines.append("|------|------|---------|--------|")
        for n in failed_nodes:
            err = (n.get('error') or '')[:80].replace('\n', ' ')
            src_url = _make_url(src_domain, n.get('node_token', ''))
            src_link = f"[查看]({src_url})" if src_url else "-"
            lines.append(f"| {n['title'][:40]} | {n['obj_type']} | {err} | {src_link} |")
        lines.append("")

    # 占位文档
    if placeholder_docs:
        lines.append("## 占位文档（mindnote/slides）\n")
        lines.append("这些类型无 API 支持自动迁移，已创建占位文档，需手动处理。\n")
        lines.append("| 文档 | 类型 | 源文档 | 目标占位 |")
        lines.append("|------|------|--------|---------|")
        for n in placeholder_docs:
            src_url = _make_url(src_domain, n.get('node_token', ''))
            tgt_url = _make_url(tgt_domain, n.get('target_node_token', ''))
            src_link = f"[查看]({src_url})" if src_url else "-"
            tgt_link = f"[查看]({tgt_url})" if tgt_url else "-"
            lines.append(f"| {n['title'][:40]} | {n['obj_type']} | {src_link} | {tgt_link} |")
        lines.append("")

    # 完全迁移
    lines.append(f"## 完全迁移（共 {len(clean_docs)} 个）\n")
    if len(clean_docs) > 30:
        lines.append("<details><summary>展开查看</summary>\n")
    lines.append("| 文档 | 类型 | 源文档 | 目标文档 |")
    lines.append("|------|------|--------|---------|")
    for n in clean_docs:
        src_url = _make_url(src_domain, n.get('node_token', ''))
        tgt_url = _make_url(tgt_domain, n.get('target_node_token', ''))
        src_link = f"[查看]({src_url})" if src_url else "-"
        tgt_link = f"[查看]({tgt_url})" if tgt_url else "-"
        lines.append(f"| {n['title'][:40]} | {n['obj_type']} | {src_link} | {tgt_link} |")
    if len(clean_docs) > 30:
        lines.append("\n</details>")
    lines.append("")

    report_text = "\n".join(lines)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report_text)

    log(f"\nReport saved: {report_path}")
    log(f"  完全迁移: {len(clean_docs)}, 有占位符: {len(problem_docs)}, "
        f"失败: {len(failed_nodes)}, 占位文档: {len(placeholder_docs)}")
    return 0


# ============================================================
# migrate-one：无需 scan 的单文档迁移
# ============================================================

def _query_node_info(node_token, source_profile):
    """通过 get_node 获取单节点的完整元信息。"""
    log(f"Querying node info: {node_token}")
    try:
        r = run_lark_json(
            ["wiki", "spaces", "get_node",
             "--params", json.dumps({"token": node_token})],
            profile=source_profile, timeout=30)
    except Exception as e:
        raise MigrationError(f"failed to query node: {e}") from e

    if not r or r.get("code") != 0:
        raise MigrationError(f"get_node failed: {json.dumps(r or {}, ensure_ascii=False)[:300]}")

    nd = r.get("data", {}).get("node", {})
    if not nd:
        raise MigrationError("node not found")
    return nd


def _ensure_single_node_in_state(state, node_info):
    """确保单节点 state 中存在当前节点；若不存在则按 pending 追加。"""
    existing = find_node(state, node_info["node_token"])
    if existing:
        return existing

    state['nodes'].append(_make_state_node(node_info, depth=0))
    return state['nodes'][-1]


def _merge_single_node_meta(state, source_profile, target_profile, source_space_id, target_space_id, target_parent):
    """migrate-one 追加到已有 state 时，补齐/覆盖关键 meta。"""
    meta = state.setdefault("meta", {})
    meta["target_space_id"] = target_space_id
    meta["source_profile"] = source_profile
    meta["target_profile"] = target_profile
    if source_space_id:
        meta["source_space_id"] = source_space_id
    if target_parent:
        meta["target_root_node_token"] = target_parent


def cmd_migrate_one(args):
    """直接迁移单个节点，无需预先 scan。

    自动通过 API 查询节点信息，创建最小 state 文件，执行迁移。
    state 文件可供后续 fix-links/fix-mentions/migrate-comments 的 single 命令使用。
    """
    sp = args.source_profile
    tp = args.target_profile
    tsi = (args.target_space or "").strip()
    node_token = (args.node_token or "").strip()
    target_parent = getattr(args, 'target_parent', '') or ''
    state_path = args.state

    if not node_token:
        log("ERROR: --node-token is required for migrate-one.")
        return 1
    if not tsi:
        log("ERROR: --target-space is required for migrate-one (目标知识空间 ID).")
        return 1

    os.makedirs(WORK_DIR, exist_ok=True)

    try:
        nd = _query_node_info(node_token, sp)
    except MigrationError as e:
        log(f"ERROR: {e}")
        return 1

    obj_type = nd.get("obj_type", "")
    obj_token = nd.get("obj_token", "")
    title = nd.get("title", "")
    node_type = nd.get("node_type", "origin")
    space_id = nd.get("space_id", "")

    log(f"  [{obj_type}] {title[:60]}")
    log(f"  obj_token={obj_token}, node_type={node_type}")

    # 2. 构建最小 state（或追加到已有 state）
    if os.path.exists(state_path):
        log(f"Appending to existing state: {state_path}")
        state = load_state(state_path)
        _merge_single_node_meta(state, sp, tp, space_id, tsi, target_parent)
        # 检查是否已存在
        existing = find_node(state, node_token)
        if existing and existing['status'] == 'done':
            log(f"Node already done in state:")
            log(f"  target_node_token={existing.get('target_node_token')}")
            log(f"  target_obj_token={existing.get('target_obj_token')}")
            return 0
        _ensure_single_node_in_state(state, nd)
    else:
        log(f"Creating new state: {state_path}")
        state = _build_state(space_id, tsi, sp, tp, target_root=target_parent)
        _ensure_single_node_in_state(state, nd)

    # 如果指定了 --target-parent，覆盖 state 中的 target_root
    if target_parent:
        state['meta']['target_root_node_token'] = target_parent

    save_state(state_path, state)

    # 3. 复用 _run_single_node 执行迁移
    args.node_token = node_token
    return _run_single_node(args, state)

# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="飞书跨组织知识库迁移工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)

    sub = parser.add_subparsers(dest="command")

    # scan
    p = sub.add_parser("scan", help="扫描源知识库，生成 state 文件")
    p.add_argument("--source-profile", required=True, help="源组织 profile (e.g. seewo)")
    p.add_argument("--target-profile", required=True, help="目标组织 profile (e.g. xupt)")
    p.add_argument("--source-space", required=True, help="源知识空间 ID")
    p.add_argument("--target-space", default="", help="目标知识空间 ID")
    p.add_argument("--from-file", default="", help="从已有 JSON 文件导入节点（跳过 API 扫描）")
    p.add_argument("--target-root", default="", help="目标知识库中的挂载父节点 node_token（迁移内容放在此节点下）")
    p.add_argument("--state", default="migration-state.json", help="State 文件路径")

    # run
    p = sub.add_parser("run", help="执行迁移（自动跳过已完成节点）")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", default="", help="只迁移指定的单个节点")

    # resume (alias)
    p = sub.add_parser("resume", help="断点续传（与 run 相同）")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", default="", help="只迁移指定的单个节点")

    # verify
    p = sub.add_parser("verify", help="验证迁移结果")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--all", dest="verify_all", action="store_true", help="验证所有节点（默认只抽样10个）")

    # precheck
    p = sub.add_parser("precheck", help="预检报告：扫描所有docx内容，分析迁移能力")
    p.add_argument("--state", default="migration-state.json")

    # migrate-one（无需预先 scan，凭 node_token 拉取节点信息后迁移）
    p = sub.add_parser(
        "migrate-one",
        help="迁移单个节点：get_node 建/并 state 后执行，无需全量 scan",
    )
    p.add_argument("--source-profile", required=True, help="源组织 lark-cli profile")
    p.add_argument("--target-profile", required=True, help="目标组织 lark-cli profile")
    p.add_argument(
        "--target-space",
        required=True,
        help="目标知识空间 ID（写入 state meta.target_space_id）",
    )
    p.add_argument("--node-token", required=True, help="源知识库中要迁移的节点 node_token")
    p.add_argument(
        "--target-parent",
        default="",
        help="目标知识库中挂载父节点的 node_token（等价于 scan 的 --target-root）",
    )
    p.add_argument(
        "--state",
        default="migration-state.json",
        help="state 文件路径；不存在则创建最小 state，存在则合并/更新 meta 并追加节点",
    )

    # report
    p = sub.add_parser("report", help="生成迁移报告（Markdown）")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--source-domain", default="", help="源组织域名（如 xxx.feishu.cn），自动检测")
    p.add_argument("--target-domain", default="", help="目标组织域名（如 yyy.feishu.cn）")
    p.add_argument("--output", default="", help="报告输出路径（默认 state文件名-report.md）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "scan":
        return cmd_scan(args)
    elif args.command in ("run", "resume"):
        return cmd_run(args)
    elif args.command == "verify":
        return cmd_verify(args)
    elif args.command == "precheck":
        return cmd_precheck(args)
    elif args.command == "migrate-one":
        return cmd_migrate_one(args)
    elif args.command == "report":
        return cmd_report(args)

    return 1
