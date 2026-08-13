#!/usr/bin/env python3
"""
测试 12：descendant Test A 的 1770041 排查 —— 表格 + cell + cell 内文本/段落

变体清单：
  V1: cell.children = [text_block_id], text_block 是 bt=2 (text)
       —— test6 用的方案，1770041 schema mismatch
  V2: 不传空数组 children（只在 cell 上不写 children）
  V3: 把 cell 的 children 包到一个 paragraph 内
  V4: text_block 移除 style:{} 和 text_element_style:{}（最小裸文本）
  V5: cell.table_cell 改为 None / 不传

用法：
  python3 tools/test12_descendant_text_in_cell.py --profile xupt --doc <doc>
"""
import argparse, sys, os, uuid, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def tmp_id():
    return uuid.uuid4().hex[:22]


def try_variant(doc, profile, label, descendants, children_id):
    log(f"\n  → {label}")
    body = {"children_id": children_id, "index": -1, "descendants": descendants}
    log(f"    body 摘要：{json.dumps(body, ensure_ascii=False)[:300]}")
    try:
        r = lark_api("POST",
            f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/descendant",
            data=body, profile=profile)
        code = r.get("code", -1) if r else -1
        msg = r.get("msg", "") if r else "no resp"
        log(f"    code={code}, msg={msg}")
        return code == 0
    except Exception as e:
        log(f"    异常：{str(e)[:300]}")
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--doc",     required=True)
    args = p.parse_args()

    log("=== 测试 12：descendant Test A 的 1770041 排查 ===")

    # ---- V1：原 test6 形态 ----
    tid, cid, txid = tmp_id(), tmp_id(), tmp_id()
    desc1 = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}, "children": [txid]},
        {"block_id": txid, "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "v1", "text_element_style": {}}}],
                  "style": {}}},
    ]
    try_variant(args.doc, args.profile, "V1: cell→text(bt=2)", desc1, [tid])

    # ---- V2：text_run 不带 text_element_style ----
    tid, cid, txid = tmp_id(), tmp_id(), tmp_id()
    desc2 = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}, "children": [txid]},
        {"block_id": txid, "block_type": 2,
         "text": {"elements": [{"text_run": {"content": "v2"}}]}},
    ]
    try_variant(args.doc, args.profile, "V2: 裸 text_run", desc2, [tid])

    # ---- V3：cell 不传 children + text 不传 elements ----
    tid, cid = tmp_id(), tmp_id()
    desc3 = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}},  # 不传 children
    ]
    try_variant(args.doc, args.profile, "V3: cell 无 children", desc3, [tid])

    # ---- V4：使用 lark-cli 实测过的 cell 内 image，验证 cell+children=[block] 形态本身合法 ----
    tid, cid, iid = tmp_id(), tmp_id(), tmp_id()
    desc4 = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}, "children": [iid]},
        {"block_id": iid, "block_type": 27, "image": {}},
    ]
    try_variant(args.doc, args.profile, "V4: cell→image (对照基线，应成功)", desc4, [tid])

    # ---- V5：换成 heading2(bt=4) 而不是 text(bt=2) ----
    tid, cid, hid = tmp_id(), tmp_id(), tmp_id()
    desc5 = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}, "children": [hid]},
        {"block_id": hid, "block_type": 4,
         "heading2": {"elements": [{"text_run": {"content": "v5 heading", "text_element_style": {}}}],
                      "style": {}}},
    ]
    try_variant(args.doc, args.profile, "V5: cell→heading2(bt=4)", desc5, [tid])

    log("\n=== 总结 ===")
    log("  对比 V1/V2/V5 找出真正必要字段；V4 是 cell→image 基线（已知可行）")


if __name__ == "__main__":
    main()
