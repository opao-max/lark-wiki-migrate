#!/usr/bin/env python3
"""测试 Block API 无法创建画板/思维导图/电子表格块。"""

import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--doc", required=True, help="目标文档 document_id")
    args = p.parse_args()

    tests = [
        (43, "画板 board",       {}),
        (29, "思维导图 mindnote", {}),
        (30, "电子表格 sheet",   {"sheet": {"token": "fake"}}),
    ]

    for bt, name, extra in tests:
        block = {"block_type": bt, **extra}
        log(f"\n尝试创建 bt={bt} ({name})")
        try:
            r = lark_api("POST",
                f"/open-apis/docx/v1/documents/{args.doc}/blocks/{args.doc}/children",
                data={"children": [block], "index": -1},
                profile=args.profile)
            log(f"  code={r.get('code')}, msg={r.get('msg')}")
        except Exception as e:
            code = re.search(r'"code":\s*(\d+)', str(e))
            msg = re.search(r'"message":\s*"([^"]+)"', str(e))
            log(f"  code={code.group(1) if code else '?'}")
            log(f"  msg={msg.group(1) if msg else str(e)[:120]}")


if __name__ == "__main__":
    main()
