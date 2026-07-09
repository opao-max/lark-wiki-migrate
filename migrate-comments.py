#!/usr/bin/env python3
"""
飞书跨组织知识库 — 评论迁移工具

将源组织文档的评论迁移到目标组织对应文档。
使用 drive file.comments create_v2 API。
- 锚定评论（划词评论）通过 quote 文字匹配目标文档 block_id，实现精细化锚定
- 回复链扁平化：每条回复成为独立评论
- 格式：作者名：评论内容

用法:
  python migrate-comments.py single --state migration-state.json --node-token <token>
  python migrate-comments.py all --state migration-state.json
  python migrate-comments.py all --state migration-state.json --dry-run
"""

import json
import time
import os
import sys
import re
import argparse

from common import log, run_lark_json, load_state, atomic_write_json

# ============================================================
# Comment reading (SOURCE)
# ============================================================

def read_comments(obj_token, file_type, profile):
    """读取源文档的所有评论（使用 --page-all 自动翻页）"""
    try:
        data = run_lark_json(
            ["drive", "file.comments", "list",
             "--params", json.dumps({"file_token": obj_token, "file_type": file_type}),
             "--page-all"],
            profile=profile, timeout=30)
    except Exception as e:
        log(f"  WARN: read comments failed for {obj_token}: {e}")
        return []

    if not data:
        return []
    return data.get("data", {}).get("items", [])

# ============================================================
# Comment writing (TARGET)
# ============================================================

def format_reply_text(reply, user_name_map, idx, total_replies):
    """格式化单条评论/回复为 "作者名：内容" 格式。

    评论中的 mention_user 和 link 元素都转为纯文本。
    idx=0 为主评论，idx>0 为回复。
    """
    user_id = reply.get("user_id", "")
    author = user_name_map.get(user_id, user_id or "未知用户")

    # Extract text content
    elements = reply.get("content", {}).get("elements", [])
    parts = []
    for elem in elements:
        if elem.get("type") == "text_run":
            parts.append(elem.get("text_run", {}).get("text", ""))
        elif elem.get("type") == "mention_user":
            mu = elem.get("mention_user", {})
            parts.append(f"@{mu.get('text', '未知用户')}")
        elif elem.get("type") == "link":
            parts.append(elem.get("link", {}).get("text", ""))
    content = "".join(parts)

    if idx == 0:
        return f"{author}：{content}"
    else:
        return f"{author}（回复）：{content}"


def create_comment(target_token, file_type, text, idempotency_key, profile,
                   anchor_block_id=None):
    """在目标文档上创建评论。

    anchor_block_id: 可选，指定评论锚定到哪个 block（划词评论）。
    idempotency_key: 幂等键，防止重复创建。
    """
    try:
        data = {
            "file_type": file_type,
            "idempotency_key": idempotency_key,
            "reply_elements": [{"type": "text", "text": text}]
        }
        if anchor_block_id:
            data["anchor"] = {"block_id": anchor_block_id}

        result = run_lark_json(
            ["drive", "file.comments", "create_v2",
             "--params", json.dumps({"file_token": target_token}),
             "--data", json.dumps(data, ensure_ascii=False)],
            profile=profile, timeout=15)
        return result
    except Exception as e:
        log(f"    WARN: create comment failed: {e}")
        return None

# ============================================================
# User name resolution
# ============================================================

def resolve_user_names(user_ids, source_profile):
    """批量解析 open_id → 用户姓名（优先取中文名）"""
    name_map = {}
    for uid in user_ids:
        if not uid or uid in name_map:
            continue
        try:
            data = run_lark_json(
                ["contact", "+get-user", "--user-id", uid],
                profile=source_profile, timeout=10)
            user = (data or {}).get("data", {}).get("user", {})
            # Prefer i18n zh_cn name, then fallback to name field
            name = (user.get("i18n_name") or {}).get("zh_cn") or user.get("name")
            if name:
                name_map[uid] = name
            else:
                name_map[uid] = f"用户{uid[-6:]}"
        except Exception:
            name_map[uid] = f"用户{uid[-6:]}"
        time.sleep(0.2)
    return name_map

# ============================================================
# Target document blocks (for anchor matching)
# ============================================================

def read_target_blocks(obj_token, profile):
    """读取目标文档所有 blocks 的文本内容，返回 [(block_id, 文本)] 列表。

    用于评论锚定匹配：通过比较源评论的 quote 文字和目标 block 的文本内容，
    找到对应的 block_id 以实现精确锚定。
    """
    blocks_text = []
    page_token = None
    for _ in range(50):  # safety limit
        params = {"document_id": obj_token, "page_size": 200}
        if page_token:
            params["page_token"] = page_token
        try:
            data = run_lark_json(
                ["api", "GET", f"/open-apis/docx/v1/documents/{obj_token}/blocks",
                 "--params", json.dumps(params)],
                profile=profile, timeout=30)
        except Exception as e:
            log(f"    WARN: read target blocks failed: {e}")
            break

        if not data:
            break
        items = data.get("data", {}).get("items", [])
        for b in items:
            bid = b.get("block_id", "")
            texts = []
            # Extract text from all possible content keys
            for key in ("text", "heading1", "heading2", "heading3",
                        "heading4", "heading5", "heading6",
                        "heading7", "heading8", "heading9"):
                content = b.get(key, {})
                if content and "elements" in content:
                    for elem in content["elements"]:
                        tr = elem.get("text_run", {})
                        if tr.get("content"):
                            texts.append(tr["content"])
            if texts:
                blocks_text.append((bid, "".join(texts)))

        if not data.get("data", {}).get("has_more"):
            break
        page_token = data.get("data", {}).get("page_token")
        if not page_token:
            break

    return blocks_text


def find_block_for_quote(blocks_text, quote):
    """通过 quote 文字在目标 blocks 中查找对应的 block_id。

    先尝试精确子串匹配，失败后用前 20 个字符做模糊匹配。
    """
    if not quote or not quote.strip():
        return None
    q = quote.strip()
    # Exact substring match
    for bid, text in blocks_text:
        if q in text:
            return bid
    # Fuzzy: try trimmed quote (first 20 chars)
    if len(q) > 20:
        short = q[:20]
        for bid, text in blocks_text:
            if short in text:
                return bid
    return None

# ============================================================
# Migrate comments for one document
# ============================================================

def migrate_comments_for_doc(node, state, dry_run=False):
    """迁移单个文档的所有评论。

    流程：
    1. 读取源文档评论列表
    2. 如有锚定评论（划词评论），读取目标文档 blocks 用于匹配
    3. 解析所有评论者的用户名
    4. 逐条创建评论（已解决的跳过，锚定失败的降级为全局评论）
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    obj_type = node['obj_type']

    # Only doc/docx support comment migration
    if obj_type not in ('doc', 'docx'):
        return {"skipped": True, "reason": f"type {obj_type} not supported"}

    file_type = "docx"  # Both doc and docx use "docx" for comments API
    src_token = node['obj_token']
    tgt_token = node.get('target_obj_token')

    if not tgt_token:
        return {"skipped": True, "reason": "no target_obj_token"}

    stats = {"migrated": 0, "anchored": 0, "anchor_failed": 0,
             "skipped_solved": 0, "errors": []}

    # 1. Read source comments
    comments = read_comments(src_token, file_type, sp)
    if not comments:
        return stats

    # 2. Check if any anchored comments exist
    has_anchored = any(not c.get("is_whole", True) for c in comments)

    # 3. Read target blocks for anchor matching (only if needed)
    blocks_text = []
    if has_anchored:
        log(f"    Reading target blocks for anchor matching...")
        blocks_text = read_target_blocks(tgt_token, tp)
        log(f"    Got {len(blocks_text)} text blocks")

    # 4. Collect all user IDs
    all_user_ids = set()
    for comment in comments:
        for reply in comment.get("reply_list", {}).get("replies", []):
            uid = reply.get("user_id", "")
            if uid:
                all_user_ids.add(uid)

    # 5. Resolve user names
    user_name_map = resolve_user_names(all_user_ids, sp)

    # 6. Process each comment
    for comment in comments:
        comment_id = comment.get("comment_id", "")
        is_whole = comment.get("is_whole", True)
        is_solved = comment.get("is_solved", False)
        quote = comment.get("quote", "")

        if is_solved:
            stats["skipped_solved"] += 1
            continue

        replies = comment.get("reply_list", {}).get("replies", [])
        if not replies:
            continue

        # Find anchor block_id for non-whole (anchored) comments
        anchor_block_id = None
        if not is_whole and blocks_text:
            anchor_block_id = find_block_for_quote(blocks_text, quote)
            if anchor_block_id:
                stats["anchored"] += 1
            else:
                stats["anchor_failed"] += 1
                log(f"    WARN: no block match for quote [{quote[:40]}], fallback to global")

        for idx, reply in enumerate(replies):
            text = format_reply_text(reply, user_name_map, idx, len(replies))
            ikey = f"migrate_{comment_id}_{idx}"

            # Only anchor the first reply (the main comment)
            block_id = anchor_block_id if idx == 0 else None

            if dry_run:
                anchor_tag = f" →block:{block_id[:16]}" if block_id else " (global)"
                log(f"    DRY RUN: [{ikey}]{anchor_tag} {text[:80]}")
                stats["migrated"] += 1
            else:
                result = create_comment(tgt_token, file_type, text, ikey, tp,
                                        anchor_block_id=block_id)
                if result and result.get("code") == 0:
                    stats["migrated"] += 1
                else:
                    # If anchored comment fails, retry as global
                    if block_id:
                        log(f"    Anchored failed, retrying as global...")
                        ikey_global = f"migrate_{comment_id}_{idx}_g"
                        result = create_comment(tgt_token, file_type, text,
                                                ikey_global, tp)
                        if result and result.get("code") == 0:
                            stats["migrated"] += 1
                            continue

                    code = (result or {}).get("code", "?")
                    msg = (result or {}).get("msg", "unknown")
                    stats["errors"].append(
                        f"comment_id={comment_id} idx={idx}: code={code} {msg}")

                time.sleep(0.3)

    return stats

# ============================================================
# Commands
# ============================================================

def cmd_single(args):
    """Migrate comments for a single node"""
    state = load_state(args.state)
    node = None
    for n in state['nodes']:
        if n['node_token'] == args.node_token:
            node = n
            break

    if not node:
        log(f"ERROR: node {args.node_token} not found in state")
        return 1

    if node['status'] != 'done':
        log(f"ERROR: node {node['node_token']} status={node['status']}, expected 'done'")
        return 1

    title = node['title'][:60]
    log(f"Migrating comments: [{node['obj_type']}] {title}")

    stats = migrate_comments_for_doc(node, state, dry_run=args.dry_run)
    log(f"Result: {json.dumps(stats, ensure_ascii=False)}")
    return 0


def cmd_all(args):
    """Migrate comments for all completed doc/docx nodes"""
    state = load_state(args.state)

    eligible = [n for n in state['nodes']
                if n['status'] == 'done' and n['obj_type'] in ('doc', 'docx')]

    if not eligible:
        log("No eligible doc/docx nodes found (status=done)")
        return 0

    log(f"Found {len(eligible)} doc/docx nodes with status=done")

    total_stats = {"migrated": 0, "anchored": 0, "anchor_failed": 0,
                    "skipped_solved": 0, "errors": []}
    processed = 0

    # Track which nodes have had comments migrated
    comments_state_file = args.state.replace('.json', '-comments.json')
    comments_state = {}
    if os.path.exists(comments_state_file):
        with open(comments_state_file, 'r') as f:
            comments_state = json.load(f)

    for node in eligible:
        nt = node['node_token']

        # Skip already migrated
        if comments_state.get(nt, {}).get('status') == 'done':
            continue

        title = node['title'][:50]
        processed += 1
        log(f"[{processed}/{len(eligible)}] [{node['obj_type']}] {title}")

        try:
            stats = migrate_comments_for_doc(node, state, dry_run=args.dry_run)

            if stats.get("skipped"):
                comments_state[nt] = {"status": "skipped", "reason": stats["reason"]}
            else:
                total_stats["migrated"] += stats.get("migrated", 0)
                total_stats["anchored"] += stats.get("anchored", 0)
                total_stats["anchor_failed"] += stats.get("anchor_failed", 0)
                total_stats["skipped_solved"] += stats.get("skipped_solved", 0)
                total_stats["errors"].extend(stats.get("errors", []))
                comments_state[nt] = {"status": "done", "stats": stats}

        except Exception as e:
            log(f"  ERROR: {e}")
            comments_state[nt] = {"status": "failed", "error": str(e)[:200]}

        atomic_write_json(comments_state_file, comments_state)
        time.sleep(0.5)

    log(f"\nTotal: migrated={total_stats['migrated']}, "
        f"anchored={total_stats['anchored']}, "
        f"anchor_failed={total_stats['anchor_failed']}, "
        f"skipped_solved={total_stats['skipped_solved']}, "
        f"errors={len(total_stats['errors'])}")

    if total_stats['errors']:
        log("Errors:")
        for e in total_stats['errors'][:20]:
            log(f"  {e}")

    return 0


def main():
    parser = argparse.ArgumentParser(description="飞书评论迁移工具")
    sub = parser.add_subparsers(dest="command")

    # single node
    p = sub.add_parser("single", help="迁移单个节点的评论")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", required=True)
    p.add_argument("--dry-run", action="store_true")

    # all nodes
    p = sub.add_parser("all", help="迁移所有已完成 doc/docx 的评论")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "single":
        return cmd_single(args)
    elif args.command == "all":
        return cmd_all(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
