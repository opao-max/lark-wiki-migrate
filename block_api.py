#!/usr/bin/env python3
"""
飞书跨组织知识库迁移 — Block API 操作

docx 文档的 Block 读取、清洗、写入。

飞书 docx 文档由 Block 树组成：
- block_type=1: 页面根节点（Page）
- block_type=2: 普通文本段落
- block_type=3~11: 各级标题 (heading1~heading9)
- block_type=13: 有序列表
- block_type=14: 无序列表
- block_type=27: 图片
- block_type=31: 表格（子节点是 block_type=32 的单元格）
- block_type=30: 嵌入电子表格视图
- block_type=23: 嵌入文件附件
- ... 更多类型见 clean_block() 中的处理

职责边界：
- 本模块只做 Block 的读/清洗/写，不做媒体下载上传（在 media.py）
- 清洗时需要 media_results 来决定占位符内容，通过参数显式传入
"""

import json
import os
import subprocess
import time

from common import (
    WORK_DIR, log, lark_api, run_lark,
    MigrationError, APIError,
)

# ============================================================
# 常量
# ============================================================

# Block 中可能携带实际内容的字段名列表
BLOCK_CONTENT_FIELDS = [
    'text', 'code',
    'heading1', 'heading2', 'heading3', 'heading4',
    'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
    'bullet', 'ordered', 'quote', 'todo',
    'divider', 'image', 'table', 'file',
    'grid', 'grid_column', 'callout',
    'quote_container',
]

# 以下字段的 elements 数组可能包含 mention_user/mention_doc 元素
_ELEMENTS_FIELDS = (
    'text', 'heading1', 'heading2', 'heading3', 'heading4',
    'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
    'bullet', 'ordered', 'quote', 'todo', 'callout',
)

# ============================================================
# Block 读取
# ============================================================

def read_docx_blocks(obj_token, profile):
    """读取 docx 文档的所有 blocks（自动处理分页）。

    飞书 Block API 每页最多返回 500 个 block，
    通过 page_token 循环获取直到所有 block 读完。
    """
    all_blocks = []
    page_token = None
    while True:
        p = {"page_size": "500", "document_revision_id": "-1"}
        if page_token:
            p["page_token"] = page_token
        r = lark_api("GET", f"/open-apis/docx/v1/documents/{obj_token}/blocks",
                      params=p, profile=profile)
        if not r or r.get("code", -1) != 0:
            raise MigrationError(f"read blocks: {json.dumps(r or {}, ensure_ascii=False)[:200]}")
        all_blocks.extend(r.get("data", {}).get("items", []))
        page_token = r.get("data", {}).get("page_token")
        if not page_token:
            break
        time.sleep(0.3)
    return all_blocks

# ============================================================
# Block 元素清洗
# ============================================================

def _sanitize_elements(elements):
    """清洗 block 中的 elements 数组 —— 处理跨组织无效的引用。

    飞书 block 的文本内容存放在 elements 数组中，每个 element 可以是：
    - text_run: 普通文本（保留）
    - mention_user: @用户（open_id 跨组织无效 → 转为纯文本 "@用户ID"）
    - mention_doc: @文档（跨组织无效 → 转为带超链接的标题文本）

    转换后的 mention 由 fix-mentions.py / fix-links.py 在后处理阶段修复。
    """
    if not elements or not isinstance(elements, list):
        return elements
    result = []
    for elem in elements:
        # 清除 text_element_style 中的 comment_ids（创建 API 不接受，会报 1770001）
        for key in ("text_run", "mention_user", "mention_doc"):
            style = (elem.get(key) or {}).get("text_element_style")
            if isinstance(style, dict) and "comment_ids" in style:
                del style["comment_ids"]
        if "mention_user" in elem:
            mu = elem["mention_user"]
            user_id = mu.get("user_id", "")
            result.append({"text_run": {"content": f"@{user_id}",
                           "text_element_style": mu.get("text_element_style", {})}})
        elif "mention_doc" in elem:
            md = elem["mention_doc"]
            title = md.get("title", "文档")
            token = md.get("token", "")
            url = f"https://seewo.feishu.cn/wiki/{token}" if token else ""
            style = {"link": {"url": url}} if url else {}
            result.append({"text_run": {
                "content": f"{title}",
                "text_element_style": style
            }})
        else:
            result.append(elem)
    return result


def _sanitize_block_content(content):
    """清洗 block 内容字典中的 elements 字段"""
    if not content or not isinstance(content, dict):
        return content
    if "elements" in content:
        content["elements"] = _sanitize_elements(content["elements"])
    return content

# ============================================================
# 占位 Block 构造
# ============================================================

def _make_text_placeholder(text):
    """构造一个纯文本占位 block (block_type=2)"""
    return {"block_type": 2, "text": {
        "elements": [{"text_run": {"content": text, "text_element_style": {}}}],
        "style": {}}}


def _make_link_placeholder(desc, url):
    """构造一个带超链接的占位 block"""
    return {"block_type": 2, "text": {
        "elements": [{"text_run": {
            "content": desc,
            "text_element_style": {"link": {"url": url}}
        }}],
        "style": {}}}

# ============================================================
# Block 清洗（核心）
# ============================================================

def clean_block(block, media_results=None):
    """清洗单个 block，使其可以写入目标文档 —— Block 操作层的核心函数。

    "清洗"的含义：源文档的 block 不能直接写入目标文档，需要：
    1. 去掉只读字段（如 block_id、parent_id）
    2. 跨组织无效的内容需要降级（如 @用户 → 纯文本、嵌入多维表格 → 占位符）
    3. 根据媒体迁移结果，决定生成什么占位符

    处理流程：
    1. 先检查是否需要特殊处理（30+ 种 block 类型各有不同逻辑）
    2. 特殊类型直接返回清洗结果（占位符 / None 表示跳过）
    3. 非特殊类型 → 拷贝可写入的通用内容字段
    4. 如果没有任何可识别内容 → 生成 "不支持的内容类型" 占位符

    Args:
        block: 源文档的 block 字典（包含 block_type, block_id 等）
        media_results: {block_id: MediaResult}，来自 media.py 的处理结果。
            通过查询此字典决定占位符内容，而不是读取 block 上的隐式 flag。
            这是 media.py → block_api.py 单向数据流的接收端。

    Returns:
        清洗后的 block 字典（可直接传给写入 API），或 None 表示跳过此 block。
    """
    bt = block["block_type"]
    mr = (media_results or {}).get(block["block_id"])

    # 1. 需要特殊处理的 block 类型
    special = _handle_special_block(block, mr, media_results)
    if special != "NO_SPECIAL_HANDLER":
        return special

    # 2. 通用内容字段拷贝
    cleaned, has_content = _copy_block_content(block)

    # 3. 未识别的 block 类型 → 占位文本
    # 1=页面根, 31=表格, 32=单元格, 33=视图容器, 34=引用容器 — 结构性 block 无内容是正常的
    if not has_content and bt not in (1, 31, 32, 33, 34):
        return _make_text_placeholder(f"[不支持的内容类型: block_type={bt}]")

    return cleaned


def _handle_special_block(block, mr, media_results):
    """处理需要特殊逻辑的 block 类型（占位符 / 跳过 / 降级）。

    飞书 docx 有 50+ 种 block_type，大部分可以通用处理（拷贝内容字段），
    但以下类型需要特殊逻辑：
    - 嵌入资源类（bt=30,23,43,18,29,53）: 跨组织 token 不可用，生成占位符
    - 容器类（bt=33）: 整合子文件的迁移结果
    - 功能类（bt=35,40,42,51）: 跨组织不可用，生成说明文本
    - 已失效（bt=999）: 飞书标记的已删除内容

    每种类型的占位符格式在两条迁移路径（Block API / export-import）中保持一致。

    Args:
        block: 源 block
        mr: 当前 block 的 MediaResult（可能为 None，表示无媒体迁移结果）
        media_results: 全量结果字典（bt=33 容器需要查子 block 的结果）

    Returns:
        - 清洗后的 block dict → 正常写入
        - None → 跳过此 block（不写入目标文档）
        - "NO_SPECIAL_HANDLER" → 非特殊类型，交给通用逻辑处理
    """
    bt = block["block_type"]

    # 嵌入电子表格 (bt=30)
    if bt == 30:
        if mr and mr.success:
            return _make_text_placeholder(
                f"[嵌入电子表格: {mr.src_token}] (已迁移到文档末尾，需手动调整位置)")
        token = (block.get("sheet") or block.get("view", {})).get("token", "")
        return _make_text_placeholder(f"[嵌入表格: {token}]")

    # 视图容器 (bt=33) — 可能包含文件 block
    if bt == 33:
        if mr and mr.resource_type == "file_container" and mr.child_files:
            lines = []
            for cf in mr.child_files:
                if cf["success"]:
                    lines.append(f"[嵌入文件: {cf['name']}] (已迁移到文档末尾，需手动调整位置)")
                else:
                    lines.append(f"[嵌入文件: {cf['name']}] (迁移失败)")
            return _make_text_placeholder("\n".join(lines))
        return None

    # 嵌入文件附件 (bt=23)
    if bt == 23:
        if mr and mr.handled_by_parent:
            return None  # 已由父级 bt=33 或 bt=2 处理
        name = block.get("file", {}).get("name", "未知文件")
        if mr and mr.success:
            return _make_text_placeholder(
                f"[嵌入文件: {name}] (已迁移到文档末尾，需手动调整位置)")
        if mr and not mr.success:
            return _make_text_placeholder(f"[嵌入文件: {name}] (迁移失败)")
        return None

    # 画板 (bt=43)
    if bt == 43:
        token = block.get("board", {}).get("token", "")
        if mr and mr.success:
            return _make_text_placeholder(
                f"[画板: {token}] (缩略图已迁移到文档末尾，需手动调整位置)")
        return _make_text_placeholder(f"[画板: {token}] (画板无法跨组织自动迁移)")

    # 嵌入多维表格 (bt=18)
    if bt == 18:
        token = block.get("bitable", {}).get("token", "")
        return _make_text_placeholder(f"[嵌入多维表格: {token}] (跨组织不可用，需手动重新嵌入)")

    # 画板/图表 (bt=21)
    if bt == 21:
        dtype = block.get("diagram", {}).get("diagram_type", "")
        return _make_text_placeholder(f"[画板/图表: diagram_type={dtype}] (跨组织不可用)")

    # iframe 嵌入网页 (bt=26)
    if bt == 26:
        iframe = block.get("iframe", {}).get("component", {})
        raw_url = iframe.get("url", "")
        if raw_url:
            from urllib.parse import unquote
            url = unquote(raw_url)
            return _make_link_placeholder(f"[嵌入网页] {url}", url)
        return _make_text_placeholder("[嵌入网页] (URL 缺失)")

    # 嵌入思维导图 (bt=29)
    if bt == 29:
        token = block.get("mindnote", {}).get("token", "")
        return _make_text_placeholder(f"[嵌入思维导图: {token}] (跨组织不可用，需手动迁移)")

    # 任务 (bt=35)
    if bt == 35:
        task_id = block.get("task", {}).get("task_id", "")
        return _make_text_placeholder(f"[任务: {task_id}] (跨组织不可用)")

    # 插件 (bt=40)
    if bt == 40:
        add_ons = block.get("add_ons", {})
        record_str = add_ons.get("record", "")
        if record_str:
            try:
                record = json.loads(record_str)
                items = record.get("items", [])
                if items:
                    lines = [f"[插件内容 - {record.get('mode', '时间线')}]"]
                    for item in items:
                        t = item.get("time", "")
                        title = item.get("title", "")
                        text = item.get("text", "")
                        parts = [p for p in [t, title, text] if p]
                        lines.append("  · " + " | ".join(parts))
                    return _make_text_placeholder("\n".join(lines))
            except (json.JSONDecodeError, TypeError):
                pass
        comp_type = add_ons.get("component_type_id", "")
        return _make_text_placeholder(f"[插件: {comp_type}] (跨组织不可用)")

    # 知识库目录 (bt=42)
    if bt == 42:
        return _make_text_placeholder("[知识库目录] (已随知识库结构自动迁移)")

    # 子页面列表 (bt=51)
    if bt == 51:
        return _make_text_placeholder("[子页面列表] (已随知识库结构自动迁移)")

    # 多维表格视图引用 (bt=53)
    if bt == 53:
        ref = block.get("reference_base", {})
        token = ref.get("token", "")
        return _make_text_placeholder(f"[多维表格视图: {token}] (跨组织不可用，需手动重新嵌入)")

    # 已失效 (bt=999)
    if bt == 999:
        return _make_text_placeholder("[此内容已失效]")

    return "NO_SPECIAL_HANDLER"


def _copy_block_content(block):
    """拷贝可写入目标文档的通用内容字段，并做轻量清洗。"""
    cleaned = {"block_type": block["block_type"]}
    has_content = False
    for field in BLOCK_CONTENT_FIELDS:
        if field not in block:
            continue
        value = block[field]
        if field in _ELEMENTS_FIELDS:
            value = _sanitize_block_content(value)
        if field == 'table':
            value = _clean_table_property(value)
        # callout/quote_container 是容器 block，文字在子节点中；
        # 只保留样式属性，去掉 elements 避免文字重复
        if field in ('callout', 'quote_container') and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k != 'elements'}
        cleaned[field] = value
        has_content = True
    return cleaned, has_content


def _clean_table_property(value):
    """表格 block 仅保留可写入的 property。"""
    if not isinstance(value, dict):
        return value
    prop = value.get("property", {})
    clean_prop = {}
    for key in ('row_size', 'column_size', 'column_width', 'header_row', 'header_column'):
        if key in prop:
            clean_prop[key] = prop[key]
    return {"property": clean_prop}

# ============================================================
# Block 写入（BFS 逐层写入）
# ============================================================

def _is_failed_image(block, media_results):
    """检查 block 是否为处理失败的图片（写入会导致参数错误）。"""
    if not media_results:
        return False
    mr = media_results.get(block["block_id"])
    return mr is not None and mr.resource_type == "image" and not mr.success


def write_blocks_bfs(blocks, target_doc_id, src_page_id, tgt_profile, media_results=None):
    """按 BFS（广度优先）顺序将 blocks 逐层写入目标文档。

    为什么用 BFS？
    飞书 Block API 要求"先有父再有子"—— 创建子 block 时必须指定已存在的父 block ID。
    所以必须按层级顺序写入：先写第 1 层，再写第 2 层……

    核心机制：
    1. id_map (src_id → tgt_id)：源文档和目标文档的 block ID 不同，
       写入时需要维护映射关系，子 block 才知道应该挂在目标的哪个父节点下。

    2. 批量写入 + 降级重试：每批最多 50 个 block（API 限制）。
       如果整批失败，降级为逐个写入，以精确定位是哪个 block 有问题。

    3. 容器自动创建子节点：飞书在创建 table(31)、grid(24/25)、
       quote_container(34) 时会自动生成子 block（如表格单元格），
       需要读回这些自动生成的 ID 并建立映射。

    4. callout(19) 特殊处理：飞书创建 callout 时会自动生成一个空子 block，
       但实际内容应该由 BFS 后续层写入，所以先删除自动生成的空子 block。

    Args:
        blocks: 源文档的所有 block 列表
        target_doc_id: 目标文档 ID（同时也是目标根 block ID）
        src_page_id: 源文档根 block ID (block_type=1)
        tgt_profile: 目标组织 lark-cli profile
        media_results: {block_id: MediaResult}，传递给 clean_block

    Returns:
        成功写入的 block 数量
    """
    BATCH_SIZE = 50

    # 构建 parent_id → children 映射（源文档的树形结构）
    children_of = {}
    for b in blocks:
        pid = b.get("parent_id")
        if pid:
            children_of.setdefault(pid, []).append(b)

    # id_map: 源 block_id → 目标 block_id 的映射
    # 初始只有根节点的映射（源页面根 → 目标文档 ID）
    id_map = {src_page_id: target_doc_id}
    total = 0

    # BFS 队列，从根节点开始逐层处理
    queue = [src_page_id]
    while queue:
        nxt = []
        for src_pid in queue:
            tgt_pid = id_map.get(src_pid)
            if not tgt_pid:
                continue
            kids = children_of.get(src_pid, [])
            if not kids:
                continue

            # 跳过图片处理失败的 block（写入会导致参数错误）
            kids = [k for k in kids if not _is_failed_image(k, media_results)]
            if not kids:
                continue

            # 已被自动创建的子 block 只需加入队列
            auto_created = [k for k in kids if k["block_id"] in id_map]
            if auto_created:
                nxt.extend(k["block_id"] for k in auto_created)
                kids = [k for k in kids if k["block_id"] not in id_map]
                if not kids:
                    continue

            # 根级画板交错写入：遇到画板 block 时先 flush 普通 block，
            # 然后 media-insert 缩略图（此时末尾=正确位置），再继续
            is_root_level = (src_pid == src_page_id)
            if is_root_level and media_results:
                written = _write_root_kids_interleaved(
                    kids, target_doc_id, tgt_pid, tgt_profile,
                    id_map, media_results, nxt, BATCH_SIZE, all_blocks=blocks)
                total += written
                continue

            # 非根级：标准分批写入
            for batch_start in range(0, len(kids), BATCH_SIZE):
                batch = kids[batch_start:batch_start + BATCH_SIZE]
                paired = [(b, clean_block(b, media_results)) for b in batch]
                paired = [(b, cb) for b, cb in paired if cb is not None]
                if not paired:
                    continue
                batch = [p[0] for p in paired]
                cleaned = [p[1] for p in paired]

                try:
                    r = lark_api("POST",
                        f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{tgt_pid}/children",
                        data={"children": cleaned, "index": batch_start},
                        profile=tgt_profile)
                except (APIError, MigrationError) as batch_err:
                    if len(batch) > 1:
                        log(f"    [docx·写入] 批量创建 {len(batch)} 个子块失败，改为逐个重试…")
                        written = _write_blocks_one_by_one(
                            batch, target_doc_id, tgt_pid, batch_start,
                            tgt_profile, id_map, media_results)
                        total += written
                        nxt.extend(k["block_id"] for k in batch)
                        continue
                    raise

                # 批量成功 → 建立 ID 映射
                new_blocks = r.get("data", {}).get("children", [])
                for i, nb in enumerate(new_blocks):
                    if i < len(batch):
                        id_map[batch[i]["block_id"]] = nb["block_id"]
                        _map_container_children(
                            batch[i], nb["block_id"],
                            target_doc_id, tgt_profile, id_map)

                total += len(batch)

                if batch_start + BATCH_SIZE < len(kids):
                    time.sleep(0.3)

            nxt.extend(k["block_id"] for k in kids)

        queue = nxt

    return total


def _is_board_with_thumbnail(block, media_results):
    """判断 block 是否是有可用缩略图的画板。"""
    if not media_results or block.get("block_type") != 43:
        return False
    mr = media_results.get(block["block_id"])
    return mr is not None and mr.resource_type == "board" and mr.success and mr.local_path


def _is_file_with_local(block, media_results):
    """判断 block 是否是有本地文件待交错写入的嵌入文件。"""
    if not media_results or block.get("block_type") != 23:
        return False
    mr = media_results.get(block["block_id"])
    return mr is not None and mr.resource_type == "file" and mr.success and mr.local_path


def _needs_interleaved_insert(block, media_results):
    """判断 block 是否需要交错写入（画板或文件有 local_path）。"""
    if _is_board_with_thumbnail(block, media_results):
        return True
    if _is_file_with_local(block, media_results):
        return True
    return False


def _get_interleaved_mr(block, media_results, all_blocks):
    """获取交错写入所需的 MediaResult。

    对于 bt=33 view容器，返回其子 bt=23 文件的 MediaResult。
    对于 bt=43 画板或 bt=23 文件，直接返回自身的 MediaResult。
    """
    mr = media_results.get(block["block_id"]) if media_results else None
    if mr and mr.local_path:
        return mr
    # bt=33 容器：找子 bt=23 的 MediaResult
    if block.get("block_type") == 33 and media_results and all_blocks:
        for b in all_blocks:
            if b.get("parent_id") == block["block_id"] and b.get("block_type") == 23:
                child_mr = media_results.get(b["block_id"])
                if child_mr and child_mr.local_path:
                    return child_mr
    return None


def _write_root_kids_interleaved(kids, target_doc_id, tgt_pid, tgt_profile,
                                  id_map, media_results, nxt, batch_size, all_blocks=None):
    """根级子 block 交错写入：普通 block 批量写入，资源 block 即时 media-insert。

    将 kids 按"是否需要交错写入"分段：
    - 普通段：积攒后批量写入（标准逻辑）
    - 资源段（画板/文件有 local_path）：media-insert 到当前末尾（位置正确）

    这样资源自然出现在前后 block 之间，而非堆在文档末尾。

    Returns:
        成功写入的 block 数量
    """
    pending = []  # 积攒的普通 block
    write_index = 0  # 当前写入位置
    written_count = [0]  # 用 list 以便 closure 可修改

    def flush_pending():
        nonlocal write_index
        if not pending:
            return
        all_pending = list(pending)
        paired = [(b, clean_block(b, media_results)) for b in pending]
        paired = [(b, cb) for b, cb in paired if cb is not None]
        if not paired:
            # 所有 block 都被跳过，但仍需加入 nxt 以处理子节点
            nxt.extend(b["block_id"] for b in all_pending)
            pending.clear()
            return

        batch_blocks = [p[0] for p in paired]
        cleaned = [p[1] for p in paired]

        for i in range(0, len(cleaned), batch_size):
            chunk_blocks = batch_blocks[i:i + batch_size]
            chunk_cleaned = cleaned[i:i + batch_size]
            try:
                r = lark_api("POST",
                    f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{tgt_pid}/children",
                    data={"children": chunk_cleaned, "index": write_index},
                    profile=tgt_profile)
                new_blocks = r.get("data", {}).get("children", [])
                for j, nb in enumerate(new_blocks):
                    if j < len(chunk_blocks):
                        id_map[chunk_blocks[j]["block_id"]] = nb["block_id"]
                        _map_container_children(
                            chunk_blocks[j], nb["block_id"],
                            target_doc_id, tgt_profile, id_map)
                write_index += len(chunk_cleaned)
                written_count[0] += len(chunk_cleaned)
            except (APIError, MigrationError):
                # 降级逐个写入
                for si, single in enumerate(chunk_blocks):
                    sc = clean_block(single, media_results)
                    if sc is None:
                        continue
                    try:
                        r2 = lark_api("POST",
                            f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{tgt_pid}/children",
                            data={"children": [sc], "index": write_index},
                            profile=tgt_profile)
                        nb2 = (r2.get("data", {}).get("children") or [{}])[0]
                        if nb2.get("block_id"):
                            id_map[single["block_id"]] = nb2["block_id"]
                            _map_container_children(
                                single, nb2["block_id"],
                                target_doc_id, tgt_profile, id_map)
                        write_index += 1
                        written_count[0] += 1
                    except (APIError, MigrationError) as e:
                        log(f"    [docx·写入] 跳过问题块 {single['block_id']}: {str(e)[:80]}")

        nxt.extend(b["block_id"] for b in all_pending)
        pending.clear()

    for kid in kids:
        interleaved_mr = _get_interleaved_mr(kid, media_results, all_blocks)
        if interleaved_mr:
            # 先 flush 前面的普通 block
            flush_pending()
            # media-insert 资源到当前末尾（=正确位置）
            mr = interleaved_mr
            insert_type = "image" if mr.resource_type == "board" else "file"
            try:
                actual_name = os.path.basename(mr.local_path)
                work_dir = os.path.dirname(mr.local_path)
                run_lark(["docs", "+media-insert",
                          "--doc", target_doc_id,
                          "--file", f"./{actual_name}",
                          "--type", insert_type],
                         profile=tgt_profile, cwd=work_dir, timeout=120)
                # media-insert 可能创建不止 1 个根级 block（如 view 容器+内容），
                # 读回根节点实际 children 数来校正 write_index
                try:
                    time.sleep(0.3)
                    root_r = lark_api("GET",
                        f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{target_doc_id}",
                        profile=tgt_profile)
                    actual_children = len((root_r or {}).get("data", {}).get("block", {}).get("children", []))
                    if actual_children > 0:
                        write_index = actual_children
                    else:
                        write_index += 1
                except Exception:
                    write_index += 1
                written_count[0] += 1
                label = "画板缩略图" if mr.resource_type == "board" else f"嵌入文件「{mr.name}」"
                log(f"    [docx·写入] {label}已交错写入")
            except Exception as e:
                label = "画板缩略图" if mr.resource_type == "board" else f"嵌入文件「{mr.name}」"
                log(f"    [docx·写入] {label}交错写入失败：{e}")
            finally:
                # 清理本地文件
                try:
                    os.remove(mr.local_path)
                except Exception:
                    pass
        else:
            pending.append(kid)

    # flush 剩余
    flush_pending()
    return written_count[0]


def _write_blocks_one_by_one(batch, target_doc_id, tgt_pid, batch_start,
                              tgt_profile, id_map, media_results):
    """逐个写入 block（批量失败时的降级路径），定位问题 block。"""
    written = 0
    for si, single in enumerate(batch):
        sc = clean_block(single, media_results)
        if sc is None:
            continue
        try:
            sr = lark_api("POST",
                f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{tgt_pid}/children",
                data={"children": [sc], "index": batch_start + written},
                profile=tgt_profile)
            new_b = (sr or {}).get("data", {}).get("children", [])
            if new_b:
                id_map[single["block_id"]] = new_b[0]["block_id"]
                _map_container_children(
                    single, new_b[0]["block_id"],
                    target_doc_id, tgt_profile, id_map)
            written += 1
        except (APIError, MigrationError) as e:
            log(f"    [docx·写入] 跳过无法创建的子块（序号 {si}）"
                f" block_type={single['block_type']} block_id={single.get('block_id','')[:24]}…")
            log(f"    [docx·写入] 已清洗请求体摘要：{json.dumps(sc, ensure_ascii=False)[:300]}")
            log(f"    [docx·写入] 接口返回：{str(e)[:200]}")
            continue
        time.sleep(0.2)
    return written


def _map_container_children(src_block, new_block_id, target_doc_id, tgt_profile, id_map):
    """读回容器 block 自动创建的子节点，建立 src→tgt ID 映射。

    飞书在创建以下类型的 block 时会自动生成子 block：
    - table (31): 自动创建 row_size × column_size 个 cell block
    - grid (24) / grid_column (25): 自动创建列 block
    - quote_container (34): 自动创建子节点
    - callout (19): 自动创建一个空子 block（需要先删除，再由 BFS 写入实际内容）

    自动创建的子 block 的 ID 由飞书分配，与源文档不同，
    所以需要读回并建立 src_child_id → tgt_child_id 的映射，
    后续 BFS 处理子层时才能找到正确的目标父节点。
    """
    bt = src_block.get("block_type")

    if bt == 19:
        # callout: 删除自动生成的空子 block，让 BFS 正常写入实际内容
        try:
            tr = lark_api("GET",
                f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{new_block_id}",
                profile=tgt_profile)
            auto_children = (tr or {}).get("data", {}).get("block", {}).get("children", [])
            for auto_child_id in auto_children:
                lark_api("DELETE",
                    f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{auto_child_id}",
                    profile=tgt_profile)
            if auto_children:
                log(f"    [docx·写入] 高亮块 {new_block_id}：已删除飞书自动生成的 {len(auto_children)} 个空子块")
        except Exception as e:
            log(f"    [docx·写入] 警告：清理高亮块自动子块失败（可忽略，不影响继续写入）：{new_block_id} — {e}")

    elif bt in (24, 25, 31, 34):
        # 容器类: 建立子 block 的 ID 映射
        src_children = src_block.get("children", [])
        if not src_children and bt == 31:
            src_children = src_block.get("table", {}).get("cells", [])
        if not src_children:
            return

        try:
            tr = lark_api("GET",
                f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{new_block_id}",
                profile=tgt_profile)
            tgt_children = (tr or {}).get("data", {}).get("block", {}).get("children", [])
            if not tgt_children:
                tr2 = lark_api("GET",
                    f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{new_block_id}/children",
                    profile=tgt_profile)
                tgt_children = [c["block_id"] for c in (tr2 or {}).get("data", {}).get("items", [])]
            for ci, src_child_id in enumerate(src_children):
                if ci < len(tgt_children):
                    id_map[src_child_id] = tgt_children[ci]
            bt_name = {24: "分栏", 25: "分栏列", 31: "表格", 34: "引用容器"}.get(bt, "容器")
            log(f"    [docx·写入] {bt_name} {new_block_id}：已为 {min(len(src_children), len(tgt_children))} 个子块建立源→目标 id 映射")
        except Exception as e:
            log(f"    [docx·写入] 警告：建立容器子块映射失败 {new_block_id} — {e}")
