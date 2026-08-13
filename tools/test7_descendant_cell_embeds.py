#!/usr/bin/env python3
"""
测试 7：descendant API 在 cell 内嵌入 5 类「资源块」的能力边界。

test6 的关键发现：board(43) 可在 cell 内创建（descendant 突破 children 限制）。
本测试系统化地试遍：sheet/bitable/file/image/mindnote 在 cell 内是否同样可创建。

每种类型用「最小合法 payload」（来自官方 SDK 字段定义）：
  - sheet:    {row_size:2, column_size:2}     —— 由飞书新建
  - bitable:  {view_type:1}                   —— 由飞书新建
  - file:     {token:"", name:"placeholder"}  —— 空 token
  - image:    {}                              —— 空 image，等待后续 upload
  - mindnote: {token:""}                      —— 空 token

用法:
  python3 tools/test7_descendant_cell_embeds.py --profile xupt --doc <目标 doc>
"""
import argparse, sys, os, uuid, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def tmp_id():
    return uuid.uuid4().hex[:22]


def try_cell_embed(doc, profile, bt, name, payload):
    log(f"\n  → bt={bt} ({name}) 嵌入 cell 内")
    tid, cid, rid = tmp_id(), tmp_id(), tmp_id()
    desc = [
        {"block_id": tid, "block_type": 31,
         "table": {"property": {"row_size": 1, "column_size": 1,
                                "header_row": False, "header_column": False}},
         "children": [cid]},
        {"block_id": cid, "block_type": 32, "table_cell": {}, "children": [rid]},
        {"block_id": rid, "block_type": bt, **payload},
    ]
    body = {"children_id": [tid], "index": -1, "descendants": desc}
    try:
        r = lark_api("POST",
            f"/open-apis/docx/v1/documents/{doc}/blocks/{doc}/descendant",
            data=body, profile=profile)
        code = r.get("code", -1) if r else -1
        msg = r.get("msg", "") if r else "no resp"
        log(f"    code={code}, msg={msg}")
        if code == 0:
            rels = r.get("data", {}).get("block_id_relations", [])
            log(f"    成功！block_id_relations={len(rels)} 项")
            return True
    except Exception as e:
        log(f"    异常：{str(e)[:200]}")
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--doc",     required=True)
    args = p.parse_args()

    log("=== 测试 7：descendant + cell 内嵌入 5 类资源块 ===")

    cases = [
        (30, "sheet",    {"sheet":    {"row_size": 2, "column_size": 2}}),
        (18, "bitable",  {"bitable":  {"view_type": 1}}),  # 项目里 bt=18
        (18, "bitable-empty", {"bitable": {}}),
        (23, "file",     {"file":     {"name": "placeholder"}}),  # 不传 token
        (23, "file-with-token", {"file": {"token": ""}}),
        (27, "image",    {"image":    {}}),
        (29, "mindnote", {"mindnote": {"token": ""}}),
    ]
    results = []
    for bt, name, payload in cases:
        ok = try_cell_embed(args.doc, args.profile, bt, name, payload)
        results.append((bt, name, ok))
        time.sleep(0.5)

    log("\n=== 汇总 ===")
    for bt, name, ok in results:
        log(f"  bt={bt:>2} {name:<10} → {'✓ 可在 cell 内' if ok else '✗ 不允许'}")


if __name__ == "__main__":
    main()
