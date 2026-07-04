#!/usr/bin/env python3
"""
飞书跨组织知识库 — 文档间互链修复工具

扫描目标文档 blocks 中的 link URL，发现包含源组织 token 的链接，
用 state 文件中的 obj_mapping 替换为目标 token。

用法:
  # 扫描单个文档的跨组织链接
  python fix-links.py --state migration-state.json --node-token <source_node_token>

  # 扫描所有已完成文档
  python fix-links.py --state migration-state.json --all

  # 仅报告（不修改）
  python fix-links.py --state migration-state.json --all --dry-run
"""

import json
import time
import os
import sys
import re
import argparse
from urllib.parse import unquote

from common import log, lark_api, load_state
from migrate.doc_blocks import read_doc_blocks

# ============================================================
# Link scanning
# ============================================================

# 飞书文档 URL 中的路径模式，用于提取 token
# 支持的路径格式：/wiki/<token>, /docx/<token>, /sheets/<token> 等
# Token 通常是 15-40 位的字母数字组合
FEISHU_PATH_TYPES = re.compile(
    r'/(wiki|docx|doc|sheets|base|file|mindnotes|slides|drive/folder)'
    r'/([A-Za-z0-9]{15,40})')


def _extract_tokens_from_url(raw_url, obj_mapping, node_mapping):
    """从飞书 URL 中提取 token，查找是否有对应的目标映射。

    处理 URL 编码（percent-encoding）和 fragment (#xxx)。
    返回 [(源token, 目标token)] 列表。
    """
    url = unquote(raw_url)
    if 'feishu.cn' not in url and 'larksuite.com' not in url:
        return []
    results = []
    for m in FEISHU_PATH_TYPES.finditer(url):
        token = m.group(2)
        # Strip fragment (#xxx) from end
        if '#' in token:
            token = token.split('#')[0]
        new_token = obj_mapping.get(token) or node_mapping.get(token)
        if new_token:
            results.append((token, new_token))
    return results


def scan_block_for_links(block, obj_mapping, node_mapping):
    """扫描单个 block 中所有超链接，找出包含源组织 token 的链接。

    检查 text_run 的 link 样式和独立的 link 元素。
    """
    findings = []

    # Check text content fields for link elements
    for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                  'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                  'bullet', 'ordered', 'quote', 'todo', 'callout'):
        content = block.get(field)
        if not content or not isinstance(content, dict):
            continue
        elements = content.get('elements', [])
        for idx, elem in enumerate(elements):
            # Check text_run with link style
            tr = elem.get('text_run')
            if tr:
                style = tr.get('text_element_style', {})
                link = style.get('link', {})
                url = link.get('url', '')
                if url:
                    for old_token, new_token in _extract_tokens_from_url(url, obj_mapping, node_mapping):
                        findings.append({
                            'block_id': block['block_id'],
                            'field': field,
                            'elem_idx': idx,
                            'old_url': url,
                            'old_token': old_token,
                            'new_token': new_token,
                        })

            # Check link elements directly
            lnk = elem.get('link')
            if lnk:
                url = lnk.get('url', '')
                if url:
                    for old_token, new_token in _extract_tokens_from_url(url, obj_mapping, node_mapping):
                        findings.append({
                            'block_id': block['block_id'],
                            'field': field,
                            'elem_idx': idx,
                            'old_url': url,
                            'old_token': old_token,
                            'new_token': new_token,
                        })

    return findings


def fix_link_in_block(doc_id, block_id, elements, profile):
    """通过 PATCH API 更新 block 中的超链接 URL"""
    try:
        lark_api("PATCH",
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{block_id}",
            data={"update_text_elements": {"elements": elements}},
            profile=profile)
        return True
    except Exception as e:
        log(f"    WARN: patch block {block_id} failed: {e}")
        return False

# ============================================================
# Commands
# ============================================================

def _detect_source_domain(blocks):
    """从文档 blocks 中检测源组织域名（如 agqg3o3wxu.feishu.cn）。"""
    for b in blocks:
        for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                      'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                      'bullet', 'ordered', 'quote', 'todo', 'callout'):
            content = b.get(field)
            if not content or not isinstance(content, dict):
                continue
            for elem in content.get('elements', []):
                url = (elem.get('text_run') or {}).get('text_element_style', {}).get('link', {}).get('url', '')
                if url:
                    decoded = unquote(url)
                    m = re.match(r'https?://([a-z0-9]+\.feishu\.cn)', decoded)
                    if m:
                        return m.group(1)
    return None


def scan_doc_for_cross_links(node, state, dry_run=False, target_domain=None):
    """扫描单个目标文档中的跨组织链接，可选修复。

    读取目标文档的 blocks → 扫描所有超链接 → 将源 token 替换为目标 token。
    同时将源组织域名替换为目标组织域名。
    按 block_id 分组修复，每个 block 只 PATCH 一次。
    """
    tp = state['meta']['target_profile']
    obj_mapping = state.get('obj_mapping', {})
    node_mapping = state.get('node_mapping', {})
    tgt_obj = node.get('target_obj_token')

    if not tgt_obj:
        return {"scanned": False, "reason": "no target_obj_token"}

    # Read target document blocks
    try:
        blocks = read_doc_blocks(tgt_obj, tp)
    except Exception as e:
        return {"scanned": False, "reason": str(e)[:200]}

    if not blocks:
        return {"scanned": False, "reason": "no blocks"}

    # Scan for links
    all_findings = []
    for block in blocks:
        findings = scan_block_for_links(block, obj_mapping, node_mapping)
        all_findings.extend(findings)

    if not all_findings:
        return {"scanned": True, "links_found": 0, "links_fixed": 0}

    log(f"    Found {len(all_findings)} cross-org links")

    # 检测源域名（用于域名替换）
    source_domain = _detect_source_domain(blocks) or state['meta'].get('source_domain', '')
    tgt_domain = target_domain or state['meta'].get('target_domain', '')

    if dry_run:
        for f in all_findings[:10]:
            log(f"      {f['old_token'][:20]} → {f['new_token'][:20]}")
        if source_domain and tgt_domain:
            log(f"    Domain: {source_domain} → {tgt_domain}")
        return {"scanned": True, "links_found": len(all_findings), "links_fixed": 0}

    # Fix links (group by block_id)
    fixed = 0
    from collections import defaultdict
    by_block = defaultdict(list)
    for f in all_findings:
        by_block[f['block_id']].append(f)

    for block_id, fixes in by_block.items():
        # Find the block and rebuild its elements
        block = next((b for b in blocks if b['block_id'] == block_id), None)
        if not block:
            continue

        # Only fix blocks with a single content field (text/heading/etc)
        for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                      'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                      'bullet', 'ordered', 'quote', 'todo', 'callout'):
            content = block.get(field)
            if not content or not isinstance(content, dict):
                continue
            elements = content.get('elements', [])
            if not elements:
                continue

            # Replace URLs in matching elements
            modified = False
            for fix in fixes:
                if fix['field'] != field:
                    continue
                idx = fix['elem_idx']
                if idx >= len(elements):
                    continue
                elem = elements[idx]
                # Fix in text_run style link
                tr = elem.get('text_run')
                if tr:
                    style = tr.get('text_element_style', {})
                    link = style.get('link', {})
                    url = link.get('url', '')
                    if url:
                        # Handle both encoded and decoded URLs
                        decoded_url = unquote(url)
                        if fix['old_token'] in decoded_url:
                            new_url = decoded_url.replace(fix['old_token'], fix['new_token'])
                            # 替换域名（源组织 → 目标组织）
                            if source_domain and tgt_domain and source_domain != tgt_domain:
                                new_url = new_url.replace(source_domain, tgt_domain)
                            link['url'] = new_url
                            modified = True
                            # Also update display text if it contains the old URL
                            content_text = tr.get('content', '')
                            decoded_content = unquote(content_text) if '%' in content_text else content_text
                            if fix['old_token'] in decoded_content:
                                tr['content'] = decoded_content.replace(fix['old_token'], fix['new_token'])

            if modified:
                # Clean elements for PATCH (remove block_id-like fields)
                clean_elems = []
                for e in elements:
                    ce = {}
                    if 'text_run' in e:
                        ce['text_run'] = e['text_run']
                    elif 'mention_user' in e:
                        ce['mention_user'] = e['mention_user']
                    elif 'link' in e:
                        ce['link'] = e['link']
                    else:
                        ce = e
                    clean_elems.append(ce)

                ok = fix_link_in_block(tgt_obj, block_id, clean_elems, tp)
                if ok:
                    fixed += len(fixes)
                time.sleep(0.3)

    return {"scanned": True, "links_found": len(all_findings), "links_fixed": fixed}


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

    log(f"Scanning links: [{node['obj_type']}] {node['title'][:60]}")
    result = scan_doc_for_cross_links(node, state, dry_run=args.dry_run,
                                       target_domain=getattr(args, 'target_domain', None))
    log(f"Result: {json.dumps(result, ensure_ascii=False)}")
    return 0


def cmd_all(args):
    state = load_state(args.state)
    obj_mapping = state.get('obj_mapping', {})
    node_mapping = state.get('node_mapping', {})
    log(f"obj_mapping: {len(obj_mapping)} entries, node_mapping: {len(node_mapping)} entries")

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
            result = scan_doc_for_cross_links(node, state, dry_run=args.dry_run,
                                               target_domain=getattr(args, 'target_domain', None))
            found = result.get('links_found', 0)
            fixed = result.get('links_fixed', 0)
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
    parser = argparse.ArgumentParser(description="飞书文档间互链修复工具")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("single", help="扫描/修复单个文档")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--target-domain", help="目标组织域名（如 xxx.feishu.cn）")

    p = sub.add_parser("all", help="扫描/修复所有已完成文档")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--target-domain", help="目标组织域名（如 xxx.feishu.cn）")

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
