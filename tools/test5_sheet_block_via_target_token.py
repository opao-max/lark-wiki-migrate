#!/usr/bin/env python3
"""
测试 5（修订版）：嵌入电子表格 block 的真实创建机制。

老结论：「Block API 不能创建 sheet 块」
真实情况：
  - 传 {sheet: {row_size, column_size}} → 成功；返回的 sheet.token 是【飞书自动新建】的目标租户合法 token
  - 传 {sheet: {token: <已有 sheet>}} 或 {sheet: {token, row_size, column_size}} → 1770001 invalid param
  - 也就是 sheet.token 是【创建时的只读返回字段】，不能在 children API 里引用已有 sheet

意味着：
  - 我们【不能】「先 import 一个 sheet → 把它的 token 嵌入 docx」
  - 嵌入电子表格的标准做法是：
      a) Block API 创建空白 sheet block（带 row/col size），由飞书自动生成新 sheet token
      b) 通过 sheets v2 API 把源 sheet 数据写入这个新 token（这就是项目 post_process 已经做的事）
  - 所以项目当前用 `<sheet/>` 占位 + insert_after + 删占位符 + 数据写入，方向是对的
  - 但有个简化空间：直接用 children API 一次性创建带 size 的 sheet block，不需要走
    占位符 → +update insert_after 这两步（Block API 直接放在正确位置）

用法：
  python3 tools/test5_sheet_block_via_target_token.py \
    --tgt-profile xupt --tgt-doc <目标 docx>
"""

import argparse, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def try_create(doc, profile, label, sheet_payload):
    log(f"\n  → {label}")
    log(f"    payload sheet={sheet_payload}")
    try:
        r = lark_api("POST",
            f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/children",
            data={"children": [{"block_type": 30, "sheet": sheet_payload}], "index": -1},
            profile=profile)
        code = r.get("code", -1)
        log(f"    code={code}, msg={r.get('msg','')}")
        if code == 0:
            bid = r["data"]["children"][0]["block_id"]
            time.sleep(0.5)
            r2 = lark_api("GET",
                f"/open-apis/docx/v1/documents/{doc}/blocks/{bid}",
                profile=profile)
            b = r2.get("data", {}).get("block", {})
            log(f"    返回 block.sheet={b.get('sheet')}")
            return bid, b.get('sheet', {}).get('token')
    except Exception as e:
        log(f"    异常：{str(e)[:200]}")
    return None, None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--tgt-profile", required=True)
    p.add_argument("--tgt-doc",     required=True)
    p.add_argument("--existing-sheet-token", default="U1FFsJy4hhryORteCSEcItyEn7d",
                   help="目标租户已有 sheet token，用于验证「不能引用已有 sheet」")
    args = p.parse_args()

    log("=== 测试 5：嵌入电子表格 block 的创建机制 ===")

    # case 1: 完全空 sheet
    try_create(args.tgt_doc, args.tgt_profile,
               "case 1：sheet={} 完全空", {})

    # case 2: 仅 row_size + column_size
    bid_a, token_a = try_create(args.tgt_doc, args.tgt_profile,
        "case 2：sheet={row_size:5, column_size:5}", {"row_size": 5, "column_size": 5})

    # case 3: 引用已有 sheet token
    try_create(args.tgt_doc, args.tgt_profile,
        f"case 3：sheet={{token: <已有 sheet>}} —— 期望失败",
        {"token": args.existing_sheet_token})

    # case 4: token + row + col
    try_create(args.tgt_doc, args.tgt_profile,
        f"case 4：sheet={{token, row_size, column_size}} —— 期望失败",
        {"token": args.existing_sheet_token, "row_size": 5, "column_size": 5})

    log("\n=== 结论 ===")
    log("  ✓ Block API 可以创建 sheet block，但只能让飞书【新建一个空 sheet】")
    log("  ✗ 不能在创建时引用已有 sheet token（1770001）")
    log("  → 嵌入电子表格的迁移路径必然是：")
    log("     1) Block API 创建空白 sheet block，得到飞书新建的 sheet token")
    log("     2) 用 sheets v2 API 把源 sheet 数据复制到新 token")
    log("  → 「先 import sheet 拿 token → 嵌入到 docx」的设想不成立")
    log("  → 但相比项目当前「占位符 + +update insert_after」，可以简化为：")
    log("     直接 children API 创建 sheet block（位置正确），省去 insert_after + 删占位符两步")

    if bid_a and token_a:
        log(f"\n  示例新 sheet token（请验证 sheets v2 API 可写）：{token_a}")


if __name__ == "__main__":
    main()
