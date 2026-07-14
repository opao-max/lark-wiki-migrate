#!/usr/bin/env python3
"""
飞书跨组织知识库 — Mindnote 浏览器自动化导出+导入工具

完整流程:
  1. 从源组织导出 mindnote 为 FreeMind (.mm) 文件
  2. 在目标组织创建 mindnote 类型的 wiki 节点（替代 docx 占位）
  3. 解析 .mm 文件，通过键盘自动化将节点树写入目标 mindnote

前置条件:
  pip install playwright

用法:
  # 登录源组织
  python mindnote-export.py login --profile seewo

  # 登录目标组织
  python mindnote-export.py login --profile xupt

  # 导出单个 mindnote（下载 .mm 到本地）
  python mindnote-export.py export --state migration-state.json --node-token <token>

  # 导入单个 mindnote 到目标组织（创建真正的思维导图）
  python mindnote-export.py import --state migration-state.json --node-token <token>

  # 一键导出+导入
  python mindnote-export.py transfer --state migration-state.json --node-token <token>

  # 批量处理所有 mindnote
  python mindnote-export.py transfer --state migration-state.json --all
"""

import subprocess
import json
import time
import os
import sys
import argparse
import xml.etree.ElementTree as ET

from common import WORK_DIR, log, load_state, save_state

MINDNOTE_DIR = os.path.join(WORK_DIR, "mindnotes")

FEISHU_WIKI_URL = "https://{domain}.feishu.cn/wiki/{node_token}"

# lark-cli profile 名称到飞书子域名的映射
# lark-cli 的 profile 名称不一定和飞书 URL 中的子域名一致
PROFILE_DOMAINS = {
    "seewo": "seewo",
    "seewo-gz": "cvte-seewo",
    "xupt": "rcnwnx20zrwi",
}


def get_feishu_domain(profile):
    """Get feishu subdomain for a profile. Falls back to profile name."""
    return PROFILE_DOMAINS.get(profile, profile)


def check_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return True
    except ImportError:
        return False


def run_lark_json(args, timeout=30):
    """执行 lark-cli 命令并解析 JSON 输出。

    注意：这是 mindnote-export 专用的简化版本，直接传入完整参数列表（含 --profile），
    与 common.run_lark_json 的接口不同（后者用 profile 关键字参数）。
    """
    r = subprocess.run(
        ["lark-cli"] + args,
        capture_output=True, text=True, timeout=timeout, cwd=WORK_DIR)
    text = r.stdout.strip()
    idx = text.find('{')
    if idx < 0:
        return None
    return json.loads(text[idx:])


# ============================================================
# Login
# ============================================================

def login_feishu(profile):
    if not check_playwright():
        log("ERROR: playwright not installed. Run: pip install playwright")
        return 1

    from playwright.sync_api import sync_playwright

    session_file = os.path.join(WORK_DIR, f"session_{profile}.json")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="chrome")
        context = browser.new_context()
        page = context.new_page()

        log(f"Opening Feishu for profile '{profile}'...")
        log("请在弹出的 Chrome 窗口中登录飞书")

        page.goto(f"https://{get_feishu_domain(profile)}.feishu.cn", timeout=60000,
                  wait_until="domcontentloaded")

        while True:
            time.sleep(2)
            if 'accounts' not in page.url and 'login' not in page.url:
                break
            try:
                page.title()
            except Exception:
                break

        context.storage_state(path=session_file)
        log(f"Session saved to {session_file}")
        browser.close()

    return 0


# ============================================================
# Export: source org → .mm file
# ============================================================

def export_mindnote_via_browser(node_token, title, profile, session_file):
    """通过浏览器自动化从源组织导出思维导图为 FreeMind (.mm) 文件。

    流程：打开 wiki 页面 → 点击更多菜单 → 下载为 → FreeMind。
    依次尝试 FreeMind / OPML / PNG 格式，全部失败则截图保存。
    """
    if not check_playwright():
        log("ERROR: playwright not installed")
        return None

    from playwright.sync_api import sync_playwright

    os.makedirs(MINDNOTE_DIR, exist_ok=True)
    url = FEISHU_WIKI_URL.format(domain=get_feishu_domain(profile), node_token=node_token)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        if os.path.exists(session_file):
            context = browser.new_context(
                storage_state=session_file, accept_downloads=True)
        else:
            context = browser.new_context(accept_downloads=True)

        page = context.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            log(f"  Opening: {url}")
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(8)

            if "accounts" in page.url or "login" in page.url:
                log("  ERROR: Session expired. Run: python mindnote-export.py login")
                browser.close()
                return None

            # Find more menu button
            menu_opened = False
            right_btns = page.evaluate('''() => {
                const btns = document.querySelectorAll("button, [role=button]");
                const results = [];
                for (const el of btns) {
                    const rect = el.getBoundingClientRect();
                    if (rect.x > 1600 && rect.top < 80 && rect.width > 0 && rect.width < 60) {
                        results.push({x: rect.x + rect.width/2, y: rect.y + rect.height/2});
                    }
                }
                return results;
            }''')

            for btn in right_btns:
                page.mouse.click(btn['x'], btn['y'])
                time.sleep(1.5)
                try:
                    if page.locator('text=下载为').first.is_visible(timeout=1000):
                        menu_opened = True
                        break
                except Exception:
                    pass
                page.keyboard.press("Escape")
                time.sleep(0.5)

            if not menu_opened:
                log("  Could not find download menu. Taking screenshot.")
                path = os.path.join(MINDNOTE_DIR, f"{node_token}.png")
                page.screenshot(path=path, full_page=True)
                browser.close()
                return path

            # Hover 下载为 → click FreeMind
            page.locator('text=下载为').first.hover()
            time.sleep(1.5)

            for fmt in ['FreeMind', 'OPML', 'PNG']:
                try:
                    fmt_item = page.locator(f'text={fmt}').first
                    if fmt_item.is_visible(timeout=1000):
                        ext_map = {'FreeMind': '.mm', 'OPML': '.opml', 'PNG': '.png'}
                        save_path = os.path.join(MINDNOTE_DIR, f"{node_token}{ext_map[fmt]}")
                        with page.expect_download(timeout=30000) as dl_info:
                            fmt_item.click()
                        dl_info.value.save_as(save_path)
                        log(f"  Downloaded as {fmt}: {os.path.getsize(save_path)} bytes")
                        browser.close()
                        return save_path
                except Exception as e:
                    log(f"  {fmt} failed: {e}")

            log("  All formats failed. Screenshot fallback.")
            path = os.path.join(MINDNOTE_DIR, f"{node_token}.png")
            page.screenshot(path=path, full_page=True)
            browser.close()
            return path

        except Exception as e:
            log(f"  Browser error: {e}")
            try:
                path = os.path.join(MINDNOTE_DIR, f"{node_token}_error.png")
                page.screenshot(path=path)
            except Exception:
                pass
            browser.close()
            return None


# ============================================================
# Parse FreeMind XML → keyboard operations
# ============================================================

def parse_freemind(mm_path):
    """解析 FreeMind .mm 文件为键盘操作序列 [(文本, 深度)]。

    跳过根节点（它是标题），只处理子节点。
    每个节点只取第一行文字，限制 200 字符。
    """
    tree = ET.parse(mm_path)
    root_node = tree.getroot().find('node')

    operations = []

    def walk(node, depth):
        text = (node.get('TEXT') or '').strip()
        if text:
            # Take first line only, limit length
            first_line = text.split('\n')[0][:200]
            operations.append((first_line, depth))
        # Always recurse into children even if this node has no text
        for child in node.findall('node'):
            walk(child, depth + 1 if text else depth)

    # Skip root node (it's the title)
    for child in root_node.findall('node'):
        walk(child, 1)

    return operations


# ============================================================
# Import: create real mindnote in target org
# ============================================================

def create_target_mindnote(node, state, args):
    """在目标组织创建 mindnote 类型的 wiki 节点，替代之前的 docx 占位文档。

    如果之前已创建过占位文档，会将其子节点移到新节点下，
    并标记旧占位文档为 "[已替换]"。
    """
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    title = node['title'][:100]
    src_nt = node['node_token']

    # Determine parent
    psrc = node.get('parent_node_token', '')
    ptgt = state['node_mapping'].get(psrc, '')

    # Create mindnote node via wiki API
    data = {
        "obj_type": "mindnote",
        "node_type": "origin",
        "title": title,
    }
    if ptgt:
        data["parent_node_token"] = ptgt

    r = run_lark_json([
        "wiki", "nodes", "create",
        "--params", json.dumps({"space_id": tsi}),
        "--data", json.dumps(data),
        "--profile", tp, "--as", "user"
    ], timeout=30)

    if not r or r.get('code', -1) != 0:
        log(f"  Failed to create target mindnote: {json.dumps(r, ensure_ascii=False)[:200]}")
        return None

    nd = r.get('data', {}).get('node', {})
    tgt_nt = nd.get('node_token')
    tgt_ot = nd.get('obj_token')
    log(f"  Created target mindnote: node={tgt_nt} obj={tgt_ot}")

    # If there was a placeholder, move its children to new node
    # (Don't delete old placeholder - wiki API doesn't support it reliably.
    #  Rename it instead to mark as superseded.)
    tgt_nt_old = node.get('target_node_token')
    if tgt_nt_old and tgt_nt_old != tgt_nt:
        # Find child nodes whose parent is the old placeholder
        children_to_move = [
            n for n in state['nodes']
            if n.get('parent_node_token') == src_nt and n.get('target_node_token')
        ]
        for child in children_to_move:
            child_tgt = child['target_node_token']
            log(f"  Moving child {child['title'][:30]} to new parent")
            run_lark_json([
                "api", "POST",
                f"/open-apis/wiki/v2/spaces/{tsi}/nodes/{child_tgt}/move",
                "--data", json.dumps({"target_parent_token": tgt_nt}),
                "--profile", tp, "--as", "user"
            ], timeout=15)
            time.sleep(0.3)

        # Update old placeholder content to indicate it's been replaced
        old_obj_token = node.get('target_obj_token')
        if old_obj_token:
            md = f"# [已替换] {title}\n\n此占位文档已被真正的思维导图替代，可安全删除。\n"
            try:
                run_lark_json([
                    "docs", "+update",
                    "--doc", old_obj_token,
                    "--mode", "overwrite",
                    "--markdown", md,
                    "--profile", tp, "--as", "user"
                ], timeout=60)
            except Exception as e:
                log(f"  WARN: failed to update placeholder: {e}")

        # Update state mapping
        state['node_mapping'][src_nt] = tgt_nt

    return tgt_nt, tgt_ot


def import_mindnote_via_browser(tgt_node_token, operations, title, profile, session_file):
    """通过键盘自动化将思维导图内容写入目标组织。

    使用 Tab/Shift+Tab 控制层级深度，Enter 创建新节点，
    逐个输入节点文本。飞书思维导图没有导入 API，只能用这种方式。
    """
    if not check_playwright():
        log("ERROR: playwright not installed")
        return False

    from playwright.sync_api import sync_playwright

    url = FEISHU_WIKI_URL.format(domain=get_feishu_domain(profile), node_token=tgt_node_token)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        if os.path.exists(session_file):
            context = browser.new_context(storage_state=session_file)
        else:
            context = browser.new_context()

        page = context.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            time.sleep(8)

            if "accounts" in page.url or "login" in page.url:
                log("  ERROR: Target session expired. Run: python mindnote-export.py login --profile " + profile)
                browser.close()
                return False

            # Click "点击添加节点" to start editing
            try:
                add_btn = page.locator('text=点击添加节点').first
                if add_btn.is_visible(timeout=3000):
                    add_btn.click()
                    time.sleep(1)
            except Exception:
                # Maybe the mindnote already has content, click center
                page.mouse.click(700, 400)
                time.sleep(1)

            # Create nodes via keyboard
            prev_depth = 0
            for i, (text, depth) in enumerate(operations):
                # Navigate depth
                if depth > prev_depth:
                    for _ in range(depth - prev_depth):
                        page.keyboard.press('Tab')
                        time.sleep(0.05)
                elif depth < prev_depth:
                    for _ in range(prev_depth - depth):
                        page.keyboard.press('Shift+Tab')
                        time.sleep(0.05)

                page.keyboard.type(text, delay=5)
                time.sleep(0.05)

                if i < len(operations) - 1:
                    page.keyboard.press('Enter')
                    time.sleep(0.1)

                prev_depth = depth

                if i > 0 and i % 30 == 0:
                    log(f"    Progress: {i+1}/{len(operations)} nodes")

            # Wait for changes to save
            time.sleep(3)
            log(f"  Imported {len(operations)} nodes into target mindnote")
            browser.close()
            return True

        except Exception as e:
            log(f"  Import browser error: {e}")
            try:
                page.screenshot(path=os.path.join(MINDNOTE_DIR, f"{tgt_node_token}_import_error.png"))
            except Exception:
                pass
            browser.close()
            return False


# ============================================================
# Commands
# ============================================================

def cmd_login(args):
    return login_feishu(args.profile)


def cmd_export(args):
    """Export mindnotes from source org to .mm files."""
    state = load_state(args.state)
    sp = state['meta']['source_profile']
    session_file = os.path.join(WORK_DIR, f"session_{sp}.json")

    if not os.path.exists(session_file):
        log(f"ERROR: No session for {sp}. Run: python mindnote-export.py login --profile {sp}")
        return 1

    if args.node_token:
        nodes = [n for n in state['nodes'] if n['node_token'] == args.node_token]
    elif args.all:
        nodes = [n for n in state['nodes']
                 if n['obj_type'] == 'mindnote' and n['status'] == 'done']
    else:
        log("ERROR: specify --node-token or --all")
        return 1

    if not nodes:
        log("No matching nodes found")
        return 0

    log(f"Exporting {len(nodes)} mindnotes...")
    exported = failed = 0
    for node in nodes:
        nt = node['node_token']
        title = node['title'][:60]
        log(f"[{exported+failed+1}/{len(nodes)}] {title}")

        file_path = export_mindnote_via_browser(nt, title, sp, session_file)
        if file_path:
            exported += 1
        else:
            failed += 1
        time.sleep(1)

    log(f"\nDone: exported={exported}, failed={failed}")
    return 0 if failed == 0 else 1


def cmd_import(args):
    """Import .mm files into target org as real mindnotes."""
    state = load_state(args.state)
    tp = state['meta']['target_profile']
    session_file = os.path.join(WORK_DIR, f"session_{tp}.json")

    if not os.path.exists(session_file):
        log(f"ERROR: No session for {tp}. Run: python mindnote-export.py login --profile {tp}")
        return 1

    if args.node_token:
        nodes = [n for n in state['nodes'] if n['node_token'] == args.node_token]
    elif args.all:
        nodes = [n for n in state['nodes']
                 if n['obj_type'] == 'mindnote' and n['status'] == 'done']
    else:
        log("ERROR: specify --node-token or --all")
        return 1

    if not nodes:
        log("No matching nodes found")
        return 0

    log(f"Importing {len(nodes)} mindnotes into target...")
    imported = failed = 0
    for node in nodes:
        nt = node['node_token']
        title = node['title'][:60]
        log(f"[{imported+failed+1}/{len(nodes)}] {title}")

        mm_path = os.path.join(MINDNOTE_DIR, f"{nt}.mm")
        if not os.path.isfile(mm_path):
            log(f"  No .mm file found. Run export first.")
            failed += 1
            continue

        operations = parse_freemind(mm_path)
        if not operations:
            log(f"  Empty mindnote (no children)")
            failed += 1
            continue
        log(f"  Parsed {len(operations)} nodes from .mm")

        # Create target mindnote
        result = create_target_mindnote(node, state, args)
        if not result:
            failed += 1
            continue
        tgt_nt, tgt_ot = result

        # Import content
        ok = import_mindnote_via_browser(tgt_nt, operations, title, tp, session_file)
        if ok:
            # Update state
            for n in state['nodes']:
                if n['node_token'] == nt:
                    n['target_node_token'] = tgt_nt
                    n['target_obj_token'] = tgt_ot
                    n['target_obj_type'] = 'mindnote'
                    break
            state['node_mapping'][nt] = tgt_nt
            save_state(args.state, state)
            imported += 1
        else:
            failed += 1

        time.sleep(1)

    log(f"\nDone: imported={imported}, failed={failed}")
    return 0 if failed == 0 else 1


def cmd_transfer(args):
    """Export from source + import to target in one step."""
    state = load_state(args.state)
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    src_session = os.path.join(WORK_DIR, f"session_{sp}.json")
    tgt_session = os.path.join(WORK_DIR, f"session_{tp}.json")

    for sf, name in [(src_session, sp), (tgt_session, tp)]:
        if not os.path.exists(sf):
            log(f"ERROR: No session for {name}. Run: python mindnote-export.py login --profile {name}")
            return 1

    if args.node_token:
        nodes = [n for n in state['nodes'] if n['node_token'] == args.node_token]
    elif args.all:
        nodes = [n for n in state['nodes']
                 if n['obj_type'] == 'mindnote' and n['status'] == 'done']
    else:
        log("ERROR: specify --node-token or --all")
        return 1

    if not nodes:
        log("No matching nodes found")
        return 0

    log(f"Transferring {len(nodes)} mindnotes...")
    done = failed = 0
    for node in nodes:
        nt = node['node_token']
        title = node['title'][:60]
        log(f"[{done+failed+1}/{len(nodes)}] {title}")

        # Step 1: Export from source
        mm_path = os.path.join(MINDNOTE_DIR, f"{nt}.mm")
        if not os.path.isfile(mm_path):
            log("  Step 1: Exporting from source...")
            file_path = export_mindnote_via_browser(nt, title, sp, src_session)
            if not file_path or not file_path.endswith('.mm'):
                log("  Export failed or not .mm format")
                failed += 1
                continue
        else:
            log(f"  Using cached .mm: {mm_path}")

        # Step 2: Parse
        operations = parse_freemind(mm_path)
        if not operations:
            log("  Empty mindnote")
            failed += 1
            continue
        log(f"  Parsed {len(operations)} nodes")

        # Step 3: Create target mindnote
        log("  Creating target mindnote...")
        result = create_target_mindnote(node, state, args)
        if not result:
            failed += 1
            continue
        tgt_nt, tgt_ot = result

        # Step 4: Import content
        log("  Importing content...")
        ok = import_mindnote_via_browser(tgt_nt, operations, title, tp, tgt_session)
        if ok:
            for n in state['nodes']:
                if n['node_token'] == nt:
                    n['target_node_token'] = tgt_nt
                    n['target_obj_token'] = tgt_ot
                    n['target_obj_type'] = 'mindnote'
                    break
            state['node_mapping'][nt] = tgt_nt
            save_state(args.state, state)
            done += 1
        else:
            failed += 1

        time.sleep(1)

    log(f"\nDone: transferred={done}, failed={failed}")
    return 0 if failed == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description="飞书 Mindnote 浏览器自动化导出+导入工具",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("login", help="登录飞书（保存浏览器会话）")
    p.add_argument("--profile", required=True)

    p = sub.add_parser("export", help="从源组织导出 mindnote 为 .mm 文件")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", default="")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("import", help="将 .mm 导入目标组织（创建真正的思维导图）")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", default="")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("transfer", help="一键导出+导入（推荐）")
    p.add_argument("--state", default="migration-state.json")
    p.add_argument("--node-token", default="")
    p.add_argument("--all", action="store_true")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1
    if args.command == "login":
        return cmd_login(args)
    elif args.command == "export":
        return cmd_export(args)
    elif args.command == "import":
        return cmd_import(args)
    elif args.command == "transfer":
        return cmd_transfer(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
