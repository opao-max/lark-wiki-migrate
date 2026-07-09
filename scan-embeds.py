#!/usr/bin/env python3
"""快速扫描源知识库所有 docx/doc 文档的嵌入内容类型。

用法:
  python3 scan-embeds.py --profile seewo --space 7085240270054064156 --workers 8
"""

import subprocess
import json
import time
import os
import sys
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict

WORK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".workdir")


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_lark_json(text):
    if not text:
        return None
    text = text.strip()
    for i, ch in enumerate(text):
        if ch in ('{', '['):
            try:
                return json.loads(text[i:])
            except json.JSONDecodeError:
                continue
    return None


def run_lark_json(args, profile, timeout=60):
    cmd = ["lark-cli"] + list(args) + ["--profile", profile, "--as", "user"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=WORK_DIR)
        if r.returncode != 0:
            return None
        return parse_lark_json(r.stdout)
    except Exception:
        return None


def scan_all_nodes(space_id, profile):
    """递归扫描知识库所有节点"""
    def _scan(parent_tk, depth=0):
        if depth > 20:
            return []
        params = {"space_id": space_id}
        if parent_tk:
            params["parent_node_token"] = parent_tk
        data = run_lark_json(
            ["wiki", "nodes", "list", "--page-all",
             "--params", json.dumps(params)],
            profile=profile, timeout=120)
        if not data:
            return []
        items = data.get("data", {}).get("items", [])
        result = []
        for item in items:
            result.append(item)
            if item.get("has_child"):
                time.sleep(0.3)
                result.extend(_scan(item["node_token"], depth + 1))
        return result
    return _scan(None)


def read_blocks(obj_token, profile):
    """读取一个文档的所有 blocks"""
    all_blocks = []
    page_token = None
    for _ in range(100):
        p = {"page_size": "500", "document_revision_id": "-1"}
        if page_token:
            p["page_token"] = page_token
        r = run_lark_json(
            ["api", "GET", f"/open-apis/docx/v1/documents/{obj_token}/blocks",
             "--params", json.dumps(p)],
            profile=profile, timeout=30)
        if not r or r.get("code", -1) != 0:
            break
        all_blocks.extend(r.get("data", {}).get("items", []))
        page_token = r.get("data", {}).get("page_token")
        if not page_token:
            break
        time.sleep(0.2)
    return all_blocks


# block_type → 人类可读的中文说明
BLOCK_TYPE_NAMES = {
    1:  "页面根节点 (Page)",
    2:  "文本段落",
    3:  "标题1 (Heading1)",
    4:  "标题2",
    5:  "标题3",
    6:  "标题4",
    7:  "标题5",
    8:  "标题6",
    9:  "标题7",
    10: "标题8",
    11: "标题9",
    12: "无序列表",
    13: "有序列表",
    14: "代码块",
    15: "引用 (Quote)",
    17: "待办事项 (Todo)",
    18: "嵌入多维表格 — 文档中内嵌了一个 bitable 视图",
    19: "高亮块 (Callout) — 带背景色的提示框",
    20: "聊天卡片",
    21: "画板/图表 (Diagram) — 飞书自带的绘图",
    22: "分割线 (Divider)",
    23: "嵌入文件附件 — 文档中内嵌的文件(视频/PPT/Word等)",
    24: "Grid 容器 — 多列分栏布局的外壳",
    25: "Grid 列 — 分栏布局中的单列",
    26: "iframe 嵌入 — 嵌入的外部网页",
    27: "图片 (Image)",
    28: "嵌入开放平台小组件",
    29: "嵌入思维导图 (Mindnote)",
    30: "嵌入电子表格视图 — 文档中内嵌的 sheet 视图",
    31: "原生表格 (Table) — 文档内的表格",
    32: "表格单元格 (Table Cell)",
    33: "视图容器 (View) — 包裹嵌入内容的外壳",
    34: "引用容器 (Quote Container)",
    35: "任务 (Task) — 飞书任务卡片",
    36: "OKR",
    37: "OKR Objective",
    38: "OKR Key Result",
    39: "OKR Progress",
    40: "插件 (Add-on) — 时间线/进度条等第三方插件",
    41: "Jira 问题",
    42: "知识库目录组件 — 自动生成的目录树",
    43: "画板/白板 (Board) — 独立画板嵌入",
    44: "链接预览卡片",
    51: "子页面列表组件 — 自动显示子文档",
    53: "多维表格视图引用 — 引用 bitable 的某个视图",
    999: "未定义/已失效内容",
}


def scan_one_doc(node, profile):
    """扫描单个文档，返回 block 统计和嵌入详情"""
    obj_token = node["obj_token"]
    title = node.get("title", "")[:60]

    try:
        blocks = read_blocks(obj_token, profile)
    except Exception as e:
        return {"error": str(e), "title": title, "obj_token": obj_token}

    if not blocks:
        return {"error": "no blocks", "title": title, "obj_token": obj_token}

    result = {
        "title": title,
        "obj_token": obj_token,
        "node_token": node.get("node_token", ""),
        "block_count": len(blocks),
        "block_types": Counter(),
        "embeds": [],  # 具体的嵌入详情
        "mention_users": 0,
        "mention_docs": 0,
        "internal_links": 0,
    }

    text_fields = ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                   'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                   'bullet', 'ordered', 'quote', 'todo', 'callout')

    for block in blocks:
        bt = block.get("block_type", -1)
        result["block_types"][bt] += 1

        # 收集嵌入详情
        if bt == 27:  # 图片
            token = block.get("image", {}).get("token", "?")
            result["embeds"].append({"type": "image", "bt": 27, "token": token})
        elif bt == 30:  # 嵌入电子表格
            token = (block.get("sheet") or block.get("view", {})).get("token", "?")
            result["embeds"].append({"type": "sheet_embed", "bt": 30, "token": token})
        elif bt == 23:  # 嵌入文件
            fi = block.get("file", {})
            name = fi.get("name", "?")
            token = fi.get("token", "?")
            result["embeds"].append({"type": "file_embed", "bt": 23, "token": token, "name": name})
        elif bt == 43:  # 画板
            token = block.get("board", {}).get("token", "?")
            result["embeds"].append({"type": "board", "bt": 43, "token": token})
        elif bt == 18:  # 嵌入多维表格
            token = block.get("bitable", {}).get("token", "?")
            result["embeds"].append({"type": "bitable_embed", "bt": 18, "token": token})
        elif bt == 21:  # 图表
            dtype = block.get("diagram", {}).get("diagram_type", "?")
            result["embeds"].append({"type": "diagram", "bt": 21, "diagram_type": dtype})
        elif bt == 26:  # iframe
            url = block.get("iframe", {}).get("component", {}).get("url", "?")
            result["embeds"].append({"type": "iframe", "bt": 26, "url": url[:100]})
        elif bt == 29:  # 思维导图
            token = block.get("mindnote", {}).get("token", "?")
            result["embeds"].append({"type": "mindnote_embed", "bt": 29, "token": token})
        elif bt == 35:  # 任务
            tid = block.get("task", {}).get("task_id", "?")
            result["embeds"].append({"type": "task", "bt": 35, "task_id": tid})
        elif bt == 40:  # 插件
            comp = block.get("add_ons", {}).get("component_type_id", "?")
            result["embeds"].append({"type": "add_on", "bt": 40, "component": comp})
        elif bt == 42:  # 目录
            result["embeds"].append({"type": "wiki_catalog", "bt": 42})
        elif bt == 51:  # 子页面列表
            result["embeds"].append({"type": "sub_page_list", "bt": 51})
        elif bt == 53:  # bitable 视图引用
            token = block.get("reference_base", {}).get("token", "?")
            result["embeds"].append({"type": "bitable_view_ref", "bt": 53, "token": token})
        elif bt not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 17, 19, 22, 24, 25, 31, 32, 33, 34):
            # 未知类型
            result["embeds"].append({"type": f"unknown_bt{bt}", "bt": bt})

        # 扫描 elements 中的 mention 和链接
        for field in text_fields:
            content = block.get(field)
            if not content or not isinstance(content, dict):
                continue
            for elem in content.get("elements", []):
                if elem.get("mention_user"):
                    result["mention_users"] += 1
                if elem.get("mention_doc"):
                    result["mention_docs"] += 1
                tr = elem.get("text_run", {})
                url = tr.get("text_element_style", {}).get("link", {}).get("url", "")
                if url and ("feishu.cn" in url or "larksuite.com" in url):
                    result["internal_links"] += 1

    return result


def main():
    parser = argparse.ArgumentParser(description="扫描知识库所有 docx/doc 的嵌入内容类型")
    parser.add_argument("--profile", required=True, help="源组织 profile")
    parser.add_argument("--space", required=True, help="源知识空间 ID")
    parser.add_argument("--workers", type=int, default=6, help="并行线程数（默认 6）")
    parser.add_argument("--output", default="embed-report.json", help="报告输出路径")
    args = parser.parse_args()

    os.makedirs(WORK_DIR, exist_ok=True)

    # 1. 扫描所有节点
    log(f"Scanning wiki space {args.space}...")
    all_nodes = scan_all_nodes(args.space, args.profile)
    log(f"Found {len(all_nodes)} total nodes")

    # 筛选 docx 和 doc
    doc_nodes = [n for n in all_nodes if n.get("obj_type") in ("docx", "doc") and n.get("node_type") != "shortcut"]
    log(f"  docx/doc (non-shortcut): {len(doc_nodes)}")

    # 2. 并行扫描 blocks
    log(f"Scanning blocks with {args.workers} workers...")
    results = []
    errors = 0
    done = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(scan_one_doc, node, args.profile): node for node in doc_nodes}
        for future in as_completed(futures):
            done += 1
            try:
                r = future.result()
                if "error" in r:
                    errors += 1
                results.append(r)
            except Exception as e:
                errors += 1
                results.append({"error": str(e)})
            if done % 50 == 0:
                log(f"  [{done}/{len(doc_nodes)}] scanned...")

    log(f"Scan done: {len(results)} docs, {errors} errors")

    # 3. 汇总统计
    total_blocks = sum(r.get("block_count", 0) for r in results if "block_count" in r)
    global_bt_counter = Counter()
    global_embed_counter = Counter()  # embed type → count
    embed_samples = defaultdict(list)  # embed type → 前 3 个样例
    total_mentions_user = 0
    total_mentions_doc = 0
    total_internal_links = 0
    docs_with_embeds = 0

    for r in results:
        if "block_types" in r:
            global_bt_counter.update(r["block_types"])
        total_mentions_user += r.get("mention_users", 0)
        total_mentions_doc += r.get("mention_docs", 0)
        total_internal_links += r.get("internal_links", 0)

        embeds = r.get("embeds", [])
        if embeds:
            docs_with_embeds += 1
        for e in embeds:
            etype = e.get("type", "unknown")
            global_embed_counter[etype] += 1
            if len(embed_samples[etype]) < 3:
                sample = dict(e)
                sample["doc_title"] = r.get("title", "?")
                sample["doc_token"] = r.get("obj_token", "?")
                embed_samples[etype].append(sample)

    # 4. 输出报告
    log(f"\n{'='*70}")
    log(f"嵌入内容扫描报告 — {args.space}")
    log(f"{'='*70}")
    log(f"总文档: {len(doc_nodes)} (docx + doc)")
    log(f"总 blocks: {total_blocks}")
    log(f"含嵌入内容的文档: {docs_with_embeds}")
    log(f"扫描失败: {errors}")

    log(f"\n--- Block 类型分布 ---")
    for bt, cnt in global_bt_counter.most_common():
        name = BLOCK_TYPE_NAMES.get(bt, f"未知类型 bt={bt}")
        log(f"  bt={bt:3d}  {cnt:6d}  {name}")

    log(f"\n--- 嵌入内容统计（需要特殊处理的部分）---")
    for etype, cnt in global_embed_counter.most_common():
        log(f"\n  [{etype}] 共 {cnt} 个")
        # 中文解释
        desc = {
            "image": "图片 — 文档中插入的图片，Block API 无法直接创建，需 media-insert 上传",
            "sheet_embed": "嵌入电子表格 — 文档中内嵌的 sheet 视图（不是原生表格），需单独 export/import",
            "file_embed": "嵌入文件附件 — 文档中内嵌的文件（视频/PPT/Word/PDF等），需下载上传",
            "board": "画板/白板 — 飞书独有的可视化白板，无导出 API，无法自动迁移",
            "bitable_embed": "嵌入多维表格 — 文档中内嵌的 bitable 视图，跨组织不可用",
            "diagram": "绘图/图表 — 飞书内置的绘图工具，无导出 API",
            "iframe": "嵌入网页 — 通过 iframe 嵌入的外部网页，可保留 URL",
            "mindnote_embed": "嵌入思维导图 — 文档中内嵌的思维导图，跨组织不可用",
            "task": "任务卡片 — 飞书任务系统的卡片，跨组织不可用",
            "add_on": "插件 — 第三方插件（时间线/进度条等），跨组织不可用",
            "wiki_catalog": "知识库目录 — 自动生成的目录树组件",
            "sub_page_list": "子页面列表 — 自动显示子文档列表的组件",
            "bitable_view_ref": "多维表格视图引用 — 引用 bitable 某个视图",
        }.get(etype, "未知类型")
        log(f"    说明: {desc}")
        for s in embed_samples[etype]:
            doc = s.get("doc_title", "?")
            extra = ""
            if "name" in s:
                extra = f" name={s['name']}"
            elif "url" in s:
                extra = f" url={s['url'][:60]}"
            elif "token" in s:
                extra = f" token={s['token'][:20]}"
            elif "task_id" in s:
                extra = f" task_id={s['task_id'][:20]}"
            log(f"    样例: [{doc}]{extra}")

    log(f"\n--- 文档内引用统计 ---")
    log(f"  @用户提及 (mention_user): {total_mentions_user}")
    log(f"  @文档提及 (mention_doc): {total_mentions_doc}")
    log(f"  飞书内部链接: {total_internal_links}")

    # 5. 保存 JSON 报告
    report = {
        "scan_time": datetime.now().isoformat(),
        "space_id": args.space,
        "total_docs": len(doc_nodes),
        "total_blocks": total_blocks,
        "docs_with_embeds": docs_with_embeds,
        "errors": errors,
        "block_type_counts": {str(k): v for k, v in global_bt_counter.most_common()},
        "embed_counts": dict(global_embed_counter.most_common()),
        "embed_samples": {k: v for k, v in embed_samples.items()},
        "mention_users": total_mentions_user,
        "mention_docs": total_mentions_doc,
        "internal_links": total_internal_links,
    }
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    log(f"\n详细报告已保存: {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
