#!/usr/bin/env python3
"""测试表格内嵌套文件：表格结构可正常迁移，但嵌套文件只能到文档末尾。

源文档 https://agqg3o3wxu.feishu.cn/wiki/UtLowtqfoiEC2lkef4Pcnc3vnQg
结构：
  Page
    ├── Text "test"
    ├── Table 2×3（某单元格内有文件 "调度资源异常-大任务.docx"）
    ├── File "希沃大数据.mm"（根级）
    └── Text "test"

流程：
  1. 读源文档，展示结构
  2. 用 Block API 将表格写入目标文档（表格结构可创建）
  3. 下载嵌套文件，media-insert 到目标文档
  4. 读目标文档验证：表格在，文件在末尾（不在表格内）
"""

import argparse, sys, os, json, subprocess, glob as _glob, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import log, lark_api, WORK_DIR
from block_api import read_docx_blocks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-profile", required=True)
    p.add_argument("--target-profile", required=True)
    p.add_argument("--source-doc", required=True)
    p.add_argument("--target-doc", required=True)
    args = p.parse_args()
    os.makedirs(WORK_DIR, exist_ok=True)

    # 1. 读源文档
    log("=== 步骤1：读取源文档结构 ===")
    blocks = read_docx_blocks(args.source_doc, args.source_profile)
    bmap = {b["block_id"]: b for b in blocks}

    table_block = None
    nested_file = None
    for b in blocks:
        bt = b.get("block_type")
        if bt == 31:
            table_block = b
            prop = b.get("table", {}).get("property", {})
            log(f"  表格: {prop.get('row_size')}×{prop.get('column_size')}")
        if bt == 23:
            parent = bmap.get(b.get("parent_id"), {})
            pbt = parent.get("block_type", "?")
            name = b.get("file", {}).get("name", "?")
            is_nested = pbt != 1
            log(f"  文件 \"{name}\" parent_bt={pbt} {'← 嵌套' if is_nested else '← 根级'}")
            if is_nested and not nested_file:
                nested_file = b

    if not table_block:
        log("未找到表格"); return
    if not nested_file:
        log("未找到嵌套文件"); return

    # 2. 用 Block API 创建表格到目标文档
    log("\n=== 步骤2：Block API 创建表格到目标文档 ===")
    table_prop = table_block.get("table", {}).get("property", {})
    clean_prop = {}
    for k in ('row_size', 'column_size', 'column_width', 'header_row', 'header_column'):
        if k in table_prop:
            clean_prop[k] = table_prop[k]

    table_data = {"block_type": 31, "table": {"property": clean_prop}}
    try:
        r = lark_api("POST",
            f"/open-apis/docx/v1/documents/{args.target_doc}/blocks/{args.target_doc}/children",
            data={"children": [table_data], "index": -1},
            profile=args.target_profile)
        code = r.get("code", -1)
        if code == 0:
            log(f"  ✓ 表格创建成功")
        else:
            log(f"  表格创建: code={code}, msg={r.get('msg')}")
    except Exception as e:
        log(f"  表格创建失败: {e}")

    time.sleep(0.5)

    # 3. 下载嵌套文件并 media-insert
    name = nested_file["file"]["name"]
    token = nested_file["file"]["token"]

    log(f"\n=== 步骤3：下载嵌套文件 \"{name}\" 并 media-insert ===")
    local = os.path.join(WORK_DIR, name)
    if os.path.exists(local):
        os.remove(local)

    for cmd in [
        ["lark-cli", "drive", "+download", "--file-token", token,
         "--output", f"./{name}", "--overwrite",
         "--profile", args.source_profile, "--as", "user"],
        ["lark-cli", "docs", "+media-download", "--token", token,
         "--output", f"./{name}",
         "--profile", args.source_profile, "--as", "user"],
    ]:
        subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=WORK_DIR)
        if os.path.exists(local):
            break

    if not os.path.exists(local):
        log("  下载失败"); return
    log(f"  下载: {name} ({os.path.getsize(local)} bytes)")

    cmd = ["lark-cli", "docs", "+media-insert",
           "--doc", args.target_doc,
           "--file", f"./{name}",
           "--type", "file",
           "--profile", args.target_profile, "--as", "user"]
    log(f"  media-insert（无位置参数，只能追加末尾）")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=WORK_DIR)
    log(f"  返回码: {r.returncode}")

    # 4. 验证
    log(f"\n=== 步骤4：验证目标文档结构 ===")
    time.sleep(1)
    tgt_blocks = read_docx_blocks(args.target_doc, args.target_profile)
    root = [b for b in tgt_blocks if b.get("parent_id") == args.target_doc]

    log(f"  根级子块（共 {len(root)} 个）：")
    for i, b in enumerate(root):
        bt = b.get("block_type")
        info = ""
        if bt == 33:
            for c in tgt_blocks:
                if c.get("parent_id") == b["block_id"] and c.get("block_type") == 23:
                    info = f'→ FILE: {c.get("file",{}).get("name","?")}'
        elif bt == 31:
            prop = b.get("table", {}).get("property", {})
            info = f'TABLE {prop.get("row_size","?")}×{prop.get("column_size","?")}'
        elif bt == 2:
            elems = b.get("text", {}).get("elements", [])
            txt = "".join(e.get("text_run", {}).get("content", "") for e in elems if "text_run" in e)
            info = f'"{txt[:30]}"' if txt else ""
        log(f"    [{i}] bt={bt} {info}")

    # 检查表格内是否有文件
    table_has_file = False
    for b in tgt_blocks:
        if b.get("block_type") == 31:
            # 遍历表格的所有后代
            def descendants(bid):
                for bb in tgt_blocks:
                    if bb.get("parent_id") == bid:
                        yield bb
                        yield from descendants(bb["block_id"])
            for d in descendants(b["block_id"]):
                if d.get("block_type") == 23:
                    table_has_file = True

    last = root[-1] if root else {}
    has_file_at_end = False
    if last.get("block_type") == 33:
        for c in tgt_blocks:
            if c.get("parent_id") == last["block_id"] and c.get("block_type") == 23:
                has_file_at_end = True

    log(f"\n  表格内有文件？{'是' if table_has_file else '否'}")
    log(f"  文件在文档末尾？{'是' if has_file_at_end else '否'}")
    if not table_has_file and has_file_at_end:
        log(f"\n  ✓ 表格结构正常迁移，但嵌套文件只能到文档末尾")
        log(f"  → Block API 可创建表格，但不能在单元格内创建文件块")
        log(f"  → media-insert 无位置参数，只能追加到文档根级末尾")

    # 清理
    if os.path.exists(local):
        os.remove(local)


if __name__ == "__main__":
    main()
