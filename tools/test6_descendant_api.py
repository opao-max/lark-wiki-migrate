#!/usr/bin/env python3
"""
测试 6：descendant API —— 一次创建多层子树。

官方 SDK 中存在但项目从未使用的接口：
  POST /open-apis/docx/v1/documents/:doc_id/blocks/:parent_id/descendant

它和我们用的 /children 区别：
  /children:    body = { children: [Block], index }       —— 只能在 :parent_id 下加一层子块
  /descendant:  body = { descendants: [Block],
                         children_id: [str], index }      —— 一次提交完整子树

descendants 是一组扁平 Block，每个 Block 自带客户端临时 block_id 和 children
引用其他 descendant 的临时 id；children_id 指定哪些 descendant 是 :parent_id 的
直接子节点；服务端会建立完整父子关系。

本测试验证两件事：
  1. 同一次请求里能否创建「表格 + 单元格 + 单元格内文本块」整棵子树
  2. 在嵌套位置（table_cell 内）能不能塞下 board/sheet 这种「服务端不允许在容器内创建的块」
     —— 如果允许，就突破了 children API 的限制；如果同样报错，
        说明限制在服务端规则，不是接口形态。

用法：
  python3 tools/test6_descendant_api.py --profile xupt --doc <目标空白文档>
"""

import argparse, sys, os, json, uuid, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def tmp_id():
    """生成 32 位客户端临时 block_id（descendant API 接受）"""
    return uuid.uuid4().hex[:22]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--doc",     required=True)
    args = p.parse_args()

    log("=== 测试 6：descendant API 一次创建多层子树 ===\n")

    # ============================================================
    # 测试 A：在根下一次创建「表格(2x2) + 4 个 cell + 第一个 cell 内一段文本」
    # ============================================================
    log("测试 A：表格 + cell + cell 内文本，一次提交")
    table_id = tmp_id()
    cell_ids = [tmp_id() for _ in range(4)]
    text_id = tmp_id()

    descendants = [
        {
            "block_id": table_id,
            "block_type": 31,
            "table": {
                "property": {"row_size": 2, "column_size": 2,
                             "header_row": False, "header_column": False}
            },
            "children": cell_ids,
        },
    ] + [
        {"block_id": cid, "block_type": 32, "table_cell": {},
         "children": ([text_id] if i == 0 else [])}
        for i, cid in enumerate(cell_ids)
    ] + [
        {"block_id": text_id, "block_type": 2,
         "text": {"elements": [
             {"text_run": {"content": "在 cell 内的文本（descendant 一次创建）",
                           "text_element_style": {}}}],
                  "style": {}}},
    ]

    body = {
        "children_id": [table_id],
        "index": -1,
        "descendants": descendants,
    }
    try:
        r = lark_api("POST",
            f"/open-apis/docx/v1/documents/{args.doc}/blocks/{args.doc}/descendant",
            data=body, profile=args.profile)
        code = r.get("code", -1) if r else -1
        msg = r.get("msg", "") if r else "no resp"
        log(f"  返回 code={code}, msg={msg}")
        if code == 0:
            data = r.get("data", {})
            log(f"  服务端返回 block_id_relations: {len(data.get('block_id_relations',[]))} 项")
            log("  ✓ 一次创建多层子树成功")
        else:
            log("  ✗ 失败")
    except Exception as e:
        log(f"  异常：{str(e)[:300]}")

    time.sleep(1)

    # ============================================================
    # 测试 B：尝试在 cell 内放一个 board(43)/sheet(30) 块（嵌入资源类）
    # 这是 children API 服务端规则的关键边界 —— 看 descendant 是否突破
    # ============================================================
    log("\n测试 B：descendant 能否在 cell 内塞下 board/sheet 块？")
    for bt, name, payload in [
        (43, "board",   {"board": {"token": ""}}),
        (30, "sheet",   {"sheet": {"token": "fake"}}),
    ]:
        log(f"\n  → 尝试 bt={bt} ({name}) 嵌入 cell")
        tid  = tmp_id()
        cid  = tmp_id()
        rid  = tmp_id()
        descendants = [
            {"block_id": tid, "block_type": 31,
             "table": {"property": {"row_size": 1, "column_size": 1,
                                    "header_row": False, "header_column": False}},
             "children": [cid]},
            {"block_id": cid, "block_type": 32, "table_cell": {},
             "children": [rid]},
            {"block_id": rid, "block_type": bt, **payload},
        ]
        body = {"children_id": [tid], "index": -1, "descendants": descendants}
        try:
            r = lark_api("POST",
                f"/open-apis/docx/v1/documents/{args.doc}/blocks/{args.doc}/descendant",
                data=body, profile=args.profile)
            code = r.get("code", -1) if r else -1
            msg = r.get("msg", "") if r else "no resp"
            log(f"    code={code}, msg={msg}")
        except Exception as e:
            log(f"    异常：{str(e)[:200]}")

    log("\n=== 结论 ===")
    log("  - 测试 A 成功 → /descendant 比 /children 更高效，可一次完成嵌套结构")
    log("  - 测试 B 若失败 → 服务端「嵌入资源类块只能在根级」的限制依然存在；")
    log("    若成功 → 我们项目「嵌套文件只能追加末尾」的所有 workaround 都可以删除")


if __name__ == "__main__":
    main()
