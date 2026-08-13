#!/usr/bin/env python3
"""测试思维导图无法通过 API 导出。

飞书导出任务 API 支持的 file_extension：docx, pdf, xlsx, csv
obj_type 支持：doc, sheet, bitable, docx
思维导图 (mindnote) 不在支持列表中。
"""

import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--mindnote-token", required=True, help="思维导图 obj_token")
    args = p.parse_args()

    log("=== 测试：对思维导图调用导出任务 API ===")
    log(f"  token: {args.mindnote_token}")

    try:
        r = lark_api("POST",
            "/open-apis/drive/v1/export_tasks",
            data={
                "file_extension": "pdf",
                "token": args.mindnote_token,
                "type": "mindnote",
            },
            profile=args.profile)
        code = r.get("code", -1)
        msg = r.get("msg", "")
        log(f"  code={code}, msg={msg}")
        if code == 0:
            log("  → 意外成功（不应发生）")
        else:
            log(f"  → 导出失败: {msg}")
    except Exception as e:
        code = re.search(r'"code":\s*(\d+)', str(e))
        msg = re.search(r'"message":\s*"([^"]+)"', str(e))
        log(f"  code={code.group(1) if code else '?'}")
        log(f"  msg={msg.group(1) if msg else str(e)[:150]}")

    log("\n结论：思维导图不在导出任务 API 支持的类型中")
    log("  支持的类型：doc, sheet, bitable, docx")
    log("  → mindnote 无法通过 API 导出/导入，只能手动操作")


if __name__ == "__main__":
    main()
