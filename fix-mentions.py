#!/usr/bin/env python3
"""
飞书跨组织知识库 — @提及修复工具

两种修复模式：
1. mention_user 元素修复：Block API 迁移保留的 mention_user，替换其中的源组织 open_id 为目标组织 open_id
2. 纯文本 @提及恢复：export/import 降级后退化为纯文本的 "@某某"，通过 batch_update 恢复为真正的 mention_user

用法:
  # 扫描单个文档的 mention
  python fix-mentions.py single --state migration-state.json --node-token <source_node_token>

  # 扫描所有已完成文档（仅报告）
  python fix-mentions.py all --state migration-state.json

  # 扫描并修复
  python fix-mentions.py all --state migration-state.json --fix
"""

import json
import time
import os
import sys
import argparse

from common import log, lark_api, load_state
from migrate.doc_blocks import read_doc_blocks

# ============================================================
# User ID mapping
# ============================================================

# 缓存：源组织 open_id → {"name": "xxx", "email": "xxx@yyy"}
# 避免对同一个用户重复调用 contact API
_user_info_cache = {}

def get_source_user_info(open_id, source_profile):
    """通过 open_id 从源组织获取用户姓名和邮箱，用于跨组织映射"""
    if open_id in _user_info_cache:
        return _user_info_cache[open_id]

    try:
        r = lark_api("GET", f"/open-apis/contact/v3/users/{open_id}",
                      params={"user_id": open_id, "user_id_type": "open_id"},
                      profile=source_profile, timeout=10)
        user = (r or {}).get("data", {}).get("user", {})
        info = {
            "name": user.get("name", open_id[:12]),
            "email": user.get("email", ""),
            "mobile": user.get("mobile", ""),
        }
    except Exception:
        info = {"name": open_id[:12], "email": "", "mobile": ""}

    _user_info_cache[open_id] = info
    time.sleep(0.2)
    return info


def get_current_user_open_id(profile):
    """Get current user's open_id in the given profile"""
    try:
        r = lark_api("GET", "/open-apis/authen/v1/user_info",
                      profile=profile, timeout=10)
        return (r or {}).get("data", {}).get("open_id")
    except Exception:
        return None


def find_target_open_id(email, mobile, name, target_profile, fallback_to_current=False):
    """在目标组织中查找用户，依次尝试邮箱 → 手机号 → 姓名搜索。

    查找优先级：邮箱最可靠 > 手机号 > 姓名（可能有重名）。
    fallback_to_current=True 时，所有方式都失败则返回当前登录用户的 open_id（用于测试）。
    """
    # Try email first (most reliable)
    if email:
        try:
            r = lark_api("POST", "/open-apis/contact/v3/users/batch_get_id",
                          data={"emails": [email]},
                          params={"user_id_type": "open_id"},
                          profile=target_profile, timeout=10)
            user_list = (r or {}).get("data", {}).get("user_list", [])
            for u in user_list:
                if u.get("user_id"):
                    return u["user_id"]
        except Exception:
            pass
        time.sleep(0.2)

    # Try mobile
    if mobile:
        try:
            r = lark_api("POST", "/open-apis/contact/v3/users/batch_get_id",
                          data={"mobiles": [mobile]},
                          params={"user_id_type": "open_id"},
                          profile=target_profile, timeout=10)
            user_list = (r or {}).get("data", {}).get("user_list", [])
            for u in user_list:
                if u.get("user_id"):
                    return u["user_id"]
        except Exception:
            pass
        time.sleep(0.2)

    # Try name search (least reliable, but useful for plain-text @mentions)
    if name:
        try:
            # Use search API to find user by name
            r = lark_api("GET", "/open-apis/search/v2/user",
                          params={"query": name, "user_id_type": "open_id", "page_size": 10},
                          profile=target_profile, timeout=10)
            users = (r or {}).get("data", {}).get("items", [])
            # Return first exact match
            for u in users:
                if u.get("name") == name and u.get("open_id"):
                    return u["open_id"]
        except Exception:
            pass
        time.sleep(0.2)

    # Fallback: use current user's open_id (useful for single-user testing)
    if fallback_to_current:
        return get_current_user_open_id(target_profile)

    return None

# ============================================================
# Block scanning & fixing
# ============================================================

def scan_mentions_in_blocks(blocks):
    """扫描 blocks 中的所有 @提及，包括两种形式：

    1. native mention_user: Block API 迁移保留的原生提及元素（含 open_id）
    2. 纯文本 "@某某": export/import 降级后退化为纯文本的提及

    返回包含 type='native' 或 type='text' 的字典列表。
    """
    import re
    mentions = []
    text_fields = ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                   'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                   'bullet', 'ordered', 'quote', 'todo', 'callout')

    for block in blocks:
        for field in text_fields:
            content = block.get(field)
            if not content or not isinstance(content, dict):
                continue
            elements = content.get('elements', [])
            for idx, elem in enumerate(elements):
                # Case 1: native mention_user element (from Block API migration)
                if 'mention_user' in elem:
                    mu = elem['mention_user']
                    uid = mu.get('user_id', '')
                    if uid:
                        mentions.append({
                            'type': 'native',
                            'block_id': block['block_id'],
                            'block': block,
                            'block_type': block.get('block_type', 0),
                            'field': field,
                            'elem_idx': idx,
                            'source_open_id': uid,
                        })
                # Case 2: plain text "@某某" (from export/import degradation)
                elif 'text_run' in elem:
                    text = elem['text_run'].get('content', '')
                    # Match @username patterns (Chinese names, English names)
                    # Typically appears as "@刘嘉欣" with colored text (text_color=5)
                    if re.match(r'^@[\w\u4e00-\u9fff]+$', text.strip()):
                        name = text.strip()[1:]  # remove @
                        mentions.append({
                            'type': 'text',
                            'block_id': block['block_id'],
                            'block': block,
                            'block_type': block.get('block_type', 0),
                            'field': field,
                            'elem_idx': idx,
                            'name': name,
                            'source_open_id': None,
                        })
    return mentions


def fix_mentions_for_doc(node, state, fix=False):
    """扫描并可选修复目标文档中的 @提及。

    fix=False 时仅报告可映射的用户数量。
    fix=True 时：
    - native mention_user: 将源 open_id 替换为目标 open_id
    - 纯文本 @提及: 转换为真正的 mention_user 元素
    - 无法映射的用户: 保留为纯文本 "@姓名"
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    tgt_obj = node.get('target_obj_token')
    src_obj = node.get('obj_token')

    if not tgt_obj:
        return {"scanned": False, "reason": "no target_obj_token"}

    try:
        blocks = read_doc_blocks(tgt_obj, tp)
    except Exception as e:
        return {"scanned": False, "reason": str(e)[:200]}

    if not blocks:
        return {"scanned": False, "reason": "no blocks"}

    mentions = scan_mentions_in_blocks(blocks)

    if not mentions:
        return {"scanned": True, "mentions_found": 0, "mentions_fixed": 0}

    native_count = sum(1 for m in mentions if m['type'] == 'native')
    text_count = sum(1 for m in mentions if m['type'] == 'text')
    log(f"    Found {len(mentions)} mentions: {native_count} native mention_user, {text_count} plain-text @mentions")

    # Build user mapping
    id_map = {}  # key → {"name", "target_open_id" or None}

    # Process native mentions
    native_ids = set(m['source_open_id'] for m in mentions if m['type'] == 'native')
    for uid in native_ids:
        info = get_source_user_info(uid, sp)
        target_id = None
        if fix:
            target_id = find_target_open_id(
                info.get("email", ""), info.get("mobile", ""),
                info.get("name", ""), tp)
        id_map[uid] = {
            "name": info["name"],
            "target_open_id": target_id,
        }

    # Process text mentions - need to find source open_id by reading source doc
    text_names = set(m['name'] for m in mentions if m['type'] == 'text')
    if text_names and src_obj:
        # Read source document to find mention_user elements with matching names
        try:
            src_blocks = read_doc_blocks(src_obj, sp)
            src_mentions = scan_mentions_in_blocks(src_blocks)
            # Build name → source_open_id mapping from source doc
            name_to_src_id = {}
            for sm in src_mentions:
                if sm['type'] == 'native':
                    src_id = sm['source_open_id']
                    info = get_source_user_info(src_id, sp)
                    name_to_src_id[info['name']] = src_id

            # Map text mentions using source open_ids
            for name in text_names:
                src_id = name_to_src_id.get(name)
                if src_id:
                    # Found source open_id, use email mapping
                    info = get_source_user_info(src_id, sp)
                    target_id = None
                    if fix:
                        target_id = find_target_open_id(
                            info.get("email", ""), info.get("mobile", ""),
                            info.get("name", ""), tp, fallback_to_current=False)
                    id_map[f"@{name}"] = {
                        "name": name,
                        "target_open_id": target_id,
                    }
                else:
                    # No source mention found, try direct name search in target
                    target_id = None
                    if fix:
                        target_id = find_target_open_id("", "", name, tp, fallback_to_current=False)
                    id_map[f"@{name}"] = {
                        "name": name,
                        "target_open_id": target_id,
                    }
        except Exception as e:
            log(f"    WARN: failed to read source doc for text mention mapping: {e}")
            # Fallback: try direct name search
            for name in text_names:
                target_id = None
                if fix:
                    target_id = find_target_open_id("", "", name, tp, fallback_to_current=False)
                id_map[f"@{name}"] = {
                    "name": name,
                    "target_open_id": target_id,
                }
    else:
        # No source doc available, try direct name search
        for name in text_names:
            target_id = None
            if fix:
                target_id = find_target_open_id("", "", name, tp, fallback_to_current=False)
            id_map[f"@{name}"] = {
                "name": name,
                "target_open_id": target_id,
            }

    if not fix:
        # Report mode
        mapped = sum(1 for v in id_map.values() if v.get("target_open_id"))
        log(f"    Can map: {mapped}/{len(id_map)} users")
        for key, info in id_map.items():
            status = "mappable" if info.get("target_open_id") else "unmapped"
            log(f"      {info['name']} ({key[:20]}): {status}")
        return {
            "scanned": True,
            "mentions_found": len(mentions),
            "mentions_fixed": 0,
            "users_total": len(id_map),
            "users_mappable": mapped,
        }

    # Fix mode: patch blocks
    fixed = 0
    from collections import defaultdict
    by_block = defaultdict(list)
    for m in mentions:
        by_block[m['block_id']].append(m)

    for block_id, block_mentions in by_block.items():
        block = block_mentions[0]['block']
        field = block_mentions[0]['field']
        content = block.get(field, {})
        elements = content.get('elements', [])
        if not elements:
            continue

        # Create new elements list with fixes
        new_elements = list(elements)
        modified = False

        for m in block_mentions:
            idx = m['elem_idx']
            if idx >= len(new_elements):
                continue

            if m['type'] == 'native':
                # Fix native mention_user: replace source open_id with target open_id
                src_id = m['source_open_id']
                info = id_map.get(src_id, {})
                tgt_id = info.get("target_open_id")

                if 'mention_user' in new_elements[idx]:
                    if tgt_id:
                        new_elements[idx]['mention_user']['user_id'] = tgt_id
                        modified = True
                        fixed += 1
                    else:
                        # Can't map → replace with text
                        name = info.get("name", src_id[:12])
                        new_elements[idx] = {
                            "text_run": {
                                "content": f"@{name}",
                                "text_element_style": new_elements[idx]['mention_user'].get('text_element_style', {})
                            }
                        }
                        modified = True

            elif m['type'] == 'text':
                # Convert plain-text @mention to native mention_user
                name = m['name']
                info = id_map.get(f"@{name}", {})
                tgt_id = info.get("target_open_id")

                if tgt_id and 'text_run' in new_elements[idx]:
                    # Replace text_run with mention_user
                    style = new_elements[idx]['text_run'].get('text_element_style', {})
                    new_elements[idx] = {
                        "mention_user": {
                            "user_id": tgt_id,
                            "text_element_style": style
                        }
                    }
                    modified = True
                    fixed += 1

        if modified:
            try:
                # Use batch_update with update_text_elements
                style = content.get('style', {})
                lark_api("PATCH",
                    f"/open-apis/docx/v1/documents/{tgt_obj}/blocks/batch_update",
                    data={"requests": [{
                        "block_id": block_id,
                        "update_text_elements": {
                            "elements": new_elements,
                            "style": style
                        }
                    }]},
                    profile=tp)
                time.sleep(0.3)
            except Exception as e:
                log(f"    WARN: patch failed for block {block_id}: {e}")

    return {"scanned": True, "mentions_found": len(mentions), "mentions_fixed": fixed}

# ============================================================
# Commands
# ============================================================

def cmd_single(args):
    state = load_state(args.state)
    node = None
    for n in state['nodes']:
        if n['node_token'] == args.node_token:
            node = n
            break
    if not node:
        log(f"ERROR: node {args.node_token} not found")
        return 1
    if node['status'] != 'done':
        log(f"ERROR: node status={node['status']}, expected 'done'")
        return 1

    log(f"Scanning mentions: [{node['obj_type']}] {node['title'][:60]}")
    result = fix_mentions_for_doc(node, state, fix=args.fix)
    log(f"Result: {json.dumps(result, ensure_ascii=False)}")
    return 0


def cmd_all(args):
    state = load_state(args.state)

    eligible = [n for n in state['nodes']
                if n['status'] == 'done' and n['obj_type'] in ('doc', 'docx')]
    log(f"Scanning {len(eligible)} doc/docx nodes...")

    total_found = 0
    total_fixed = 0
    processed = 0

    for node in eligible:
        title = node['title'][:50]
        processed += 1
        try:
            result = fix_mentions_for_doc(node, state, fix=args.fix)
            found = result.get('mentions_found', 0)
            fixed = result.get('mentions_fixed', 0)
            total_found += found
            total_fixed += fixed
            if found > 0:
                log(f"[{processed}/{len(eligible)}] {title}: found={found} fixed={fixed}")
        except Exception as e:
            log(f"[{processed}/{len(eligible)}] {title}: ERROR {e}")
        time.sleep(0.3)

    log(f"\nTotal: found={total_found} fixed={total_fixed}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="飞书@提及修复工具")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("single", help="扫描/修复单个文档")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", required=True)
    p.add_argument("--fix", action="store_true", help="实际修复（默认只报告）")

    p = sub.add_parser("all", help="扫描/修复所有文档")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--fix", action="store_true", help="实际修复（默认只报告）")
    p.add_argument("--report", action="store_true", help="仅生成报告")

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
