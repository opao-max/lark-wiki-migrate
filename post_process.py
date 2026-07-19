#!/usr/bin/env python3
"""
飞书跨组织知识库迁移 — 迁移后处理

迁移完成后的二次处理：
- 嵌入表格数据复制：将源表格的数据写入目标嵌入表格
- 占位符统一：将 export/import 路径产生的飞书自动文本替换为统一格式
"""

import json
import time

from common import (
    log, lark_api, run_lark,
)
from block_api import read_docx_blocks

# ============================================================
# 嵌入表格数据复制
# ============================================================

def post_migrate_embed_sheets(tgt_doc_id, media_results, state):
    """在文档迁移完成后，将嵌入表格插入目标文档并复制数据。

    为什么需要后处理？
    嵌入表格 (bt=30) 在 Block API 写入时无法直接创建，
    所以在主迁移流程中只生成占位符文本。
    本函数在文档写入完成后，通过以下步骤补回嵌入表格：

    流程（对每个成功迁移的 sheet）：
    1. 用 docs +update --mode append 在文档末尾插入 <sheet/> 标签
       飞书会自动创建嵌入表格视图 block
    2. 读回目标文档，找到刚插入的 bt=30 block，获取飞书实际分配的 token
       （这个 token 可能与导入时的 token 不同）
    3. 从源表格读取数据（sheets API），写入目标表格
    4. 删除目标表格中多余的空行（<sheet/> 创建时默认会有很多空行）

    Args:
        tgt_doc_id: 目标文档 ID
        media_results: {block_id: MediaResult}，来自 media.process_embedded_sheets
        state: 迁移状态（包含 source/target profile）
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']

    # 找出所有成功迁移的 sheet
    sheet_results = [r for r in media_results.values()
                     if r.resource_type == "sheet" and r.success]
    if not sheet_results:
        return

    for sr in sheet_results:
        src_compound = sr.src_token
        new_compound = sr.target_token
        if not new_compound or not src_compound:
            continue

        # 插入 <sheet/> 标签 —— 优先定位到占位符位置
        # Block API 路径的占位符: "[嵌入电子表格: {token}]..." — 天然唯一
        # Export/Import 路径的占位符: "点击图片可查看完整电子表格" — 多个表格时不唯一
        #
        # 当多个同名占位符存在时，selection-with-ellipsis 要求唯一匹配会失败。
        # 策略：先尝试唯一占位符（Block API）；如果没有，用 Block API 将第一个
        # 匹配的 block 内容改为唯一文本，再 insert_after。
        unique_placeholder = f"[嵌入电子表格: {src_compound}]"
        generic_placeholder = "点击图片可查看完整电子表格"

        positioned = False
        # 1. 尝试 Block API 路径的唯一占位符
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_doc_id,
                      "--mode", "insert_after",
                      "--selection-with-ellipsis", unique_placeholder,
                      "--markdown", f'<sheet token="{new_compound}"/>'],
                     profile=tp, timeout=30)
            positioned = True
            log(f"    [docx·后处理] 嵌入表格已定位到占位符位置")
            time.sleep(0.3)
            try:
                run_lark(["docs", "+update",
                          "--doc", tgt_doc_id,
                          "--mode", "delete_range",
                          "--selection-with-ellipsis", unique_placeholder],
                         profile=tp, timeout=30)
            except Exception:
                pass
        except Exception:
            pass

        # 2. 用 Block API 将第一个 "点击图片可查看完整电子表格" block 改为唯一文本
        if not positioned:
            try:
                positioned = _uniquify_and_position_sheet(
                    tgt_doc_id, generic_placeholder, unique_placeholder,
                    new_compound, tp)
            except Exception:
                pass

        if not positioned:
            # 所有占位符都匹配失败，fallback 到 append
            try:
                run_lark(["docs", "+update",
                          "--doc", tgt_doc_id,
                          "--mode", "append",
                          "--markdown", f'<sheet token="{new_compound}"/>'],
                         profile=tp, timeout=30)
            except Exception as e:
                log(f"    [docx·后处理] 警告：插入嵌入表格视图（<sheet/>）失败 — {e}")
                continue

        # 读回文档以获取飞书实际分配的嵌入 token
        time.sleep(0.5)
        try:
            tgt_blocks = read_docx_blocks(tgt_doc_id, tp)
            embed_blocks = [b for b in tgt_blocks if b.get("block_type") == 30]
            if not embed_blocks:
                log("    [docx·后处理] 警告：插入后未在目标文档中找到嵌入表格块（bt=30）")
                continue
            actual_embed = embed_blocks[-1]  # 取最后一个（刚刚 append 的）
            actual_token = actual_embed.get("sheet", {}).get("token", "")
            if not actual_token:
                continue

            actual_base = actual_token.rsplit('_', 1)[0] if '_' in actual_token else actual_token
            actual_sheet_id = actual_token.rsplit('_', 1)[1] if '_' in actual_token else ""

            # 从源表格读取数据并写入目标
            src_base = src_compound.rsplit('_', 1)[0] if '_' in src_compound else src_compound
            src_view = src_compound.rsplit('_', 1)[1] if '_' in src_compound else ""
            if src_view and actual_sheet_id:
                _copy_sheet_data(src_base, src_view, actual_base, actual_sheet_id, sp, tp)

        except Exception as e:
            log(f"    [docx·后处理] 警告：从源表复制数据到目标嵌入表失败 — {e}")

    log(f"  [docx·后处理] 嵌入电子表格收尾完成：共处理 {len(sheet_results)} 个已迁移的嵌入表")


def _uniquify_and_position_sheet(tgt_doc_id, generic_text, unique_text, new_compound, tp):
    """用 Block API 将第一个包含 generic_text 的 block 改为 unique_text，
    然后 insert_after + delete 定位嵌入表格。

    当文档中有多个相同占位文本时，selection-with-ellipsis 要求唯一匹配会失败。
    此函数通过 Block API 直接修改第一个匹配 block 的内容使其唯一。
    """
    tgt_blocks = read_docx_blocks(tgt_doc_id, tp)
    target_block = None
    for b in tgt_blocks:
        for field in ('text', 'heading1', 'heading2', 'heading3', 'heading4',
                       'heading5', 'heading6', 'heading7', 'heading8', 'heading9',
                       'bullet', 'ordered', 'quote', 'todo'):
            if field not in b:
                continue
            elems = b[field].get("elements", [])
            full_text = "".join(e.get("text_run", {}).get("content", "") for e in elems)
            if generic_text in full_text:
                target_block = b
                break
        if target_block:
            break

    if not target_block:
        return False

    # 用 update block API 将内容改为唯一文本
    block_id = target_block["block_id"]
    lark_api("PATCH",
        f"/open-apis/docx/v1/documents/{tgt_doc_id}/blocks/{block_id}",
        data={"update_text_elements": {"elements": [
            {"text_run": {"content": unique_text, "text_element_style": {}}}
        ]}},
        profile=tp)
    time.sleep(0.3)

    # 现在 unique_text 是唯一的，可以 insert_after
    run_lark(["docs", "+update",
              "--doc", tgt_doc_id,
              "--mode", "insert_after",
              "--selection-with-ellipsis", unique_text,
              "--markdown", f'<sheet token="{new_compound}"/>'],
             profile=tp, timeout=30)
    log(f"    [docx·后处理] 嵌入表格已定位到占位符位置（通过 Block API 唯一化）")
    time.sleep(0.3)

    # 删除唯一化后的占位符
    try:
        run_lark(["docs", "+update",
                  "--doc", tgt_doc_id,
                  "--mode", "delete_range",
                  "--selection-with-ellipsis", unique_text],
                 profile=tp, timeout=30)
    except Exception:
        pass
    return True


def _copy_sheet_data(src_base, src_view, tgt_base, tgt_sheet_id, sp, tp):
    """从源表格读取数据，写入目标表格，并删除多余空行。"""
    src_data = lark_api("GET",
        f"/open-apis/sheets/v2/spreadsheets/{src_base}/values/{src_view}",
        profile=sp)
    values = (src_data or {}).get("data", {}).get("valueRange", {}).get("values", [])
    if not values:
        return

    # 计算数据范围（最多支持 26 列 A-Z）
    max_col = max(len(row) for row in values)
    col_letter = chr(ord('A') + min(max_col - 1, 25))
    range_str = f"{tgt_sheet_id}!A1:{col_letter}{len(values)}"
    lark_api("PUT",
        f"/open-apis/sheets/v2/spreadsheets/{tgt_base}/values",
        data={"valueRange": {"range": range_str, "values": values}},
        profile=tp)
    log(f"    [docx·后处理] 已写入表格数据：{len(values)} 行")

    # 删除多余空行
    _trim_extra_rows(tgt_base, tgt_sheet_id, len(values), tp)


def _trim_extra_rows(tgt_base, tgt_sheet_id, data_rows, tp):
    """删除 <sheet/> 标签创建时多出的空行。

    飞书 sheets DELETE dimension_range API 使用 1-based inclusive 索引：
    startIndex=N, endIndex=M 删除第 N 到第 M 行（含两端）。
    """
    try:
        meta = lark_api("GET",
            f"/open-apis/sheets/v2/spreadsheets/{tgt_base}/metainfo",
            profile=tp)
        for s in (meta or {}).get("data", {}).get("sheets", []):
            if s.get("sheetId") == tgt_sheet_id:
                total_rows = s.get("rowCount", 0)
                if total_rows > data_rows:
                    run_lark(
                        ["api", "DELETE",
                         f"/open-apis/sheets/v2/spreadsheets/{tgt_base}/dimension_range",
                         "--data", json.dumps({
                             "dimension": {
                                 "sheetId": tgt_sheet_id,
                                 "majorDimension": "ROWS",
                                 "startIndex": data_rows + 1,
                                 "endIndex": total_rows,
                             }
                         })],
                        profile=tp, timeout=30)
                    log(f"    [docx·后处理] 已删除多余空行：{total_rows - data_rows} 行")
                break
    except Exception as e:
        log(f"    [docx·后处理] 警告：删除嵌入表多余空行失败 — {e}")

# ============================================================
# 嵌入文件 / 画板缩略图位置复位
# ============================================================

def post_reposition_files(tgt_doc_id, media_results, tgt_profile):
    """将文档末尾的嵌入文件复位到占位符位置。

    流程：
    1. 记录复位前末尾原始文件块的 block_id
    2. 对每个文件：insert_after 占位符创建副本 + delete_range 删占位符
    3. 按 block_id 精确删除末尾原始文件块
    """
    file_results = [r for r in media_results.values()
                    if r.resource_type == "file" and r.success
                    and r.target_token]
    if not file_results:
        return

    # 记录复位前末尾原始文件块的 block_id（bt=33 view 容器）
    original_view_ids = []
    try:
        pre_blocks = read_docx_blocks(tgt_doc_id, tgt_profile)
        root_children = [b for b in pre_blocks
                         if b.get("parent_id") == tgt_doc_id]
        # 从末尾向前收集 bt=33，直到遇到非 bt=33 就停
        # （原始文件块一定在末尾连续排列）
        for b in reversed(root_children):
            if b.get("block_type") == 33:
                original_view_ids.append(b["block_id"])
            elif b.get("block_type") == 27:
                # 画板缩略图（IMAGE）可能插在文件之间，跳过继续找
                continue
            else:
                break
    except Exception:
        pass

    repositioned = 0
    for fr in file_results:
        placeholder = f"[嵌入文件: {fr.name}] (已迁移到文档末尾，需手动调整位置)"

        # 1. insert_after 在占位符后创建文件副本
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_doc_id,
                      "--mode", "insert_after",
                      "--selection-with-ellipsis", placeholder,
                      "--markdown", f'<file token="{fr.target_token}"/>'],
                     profile=tgt_profile, timeout=30)
        except Exception:
            continue

        # 2. 删除占位符文字
        time.sleep(0.3)
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_doc_id,
                      "--mode", "delete_range",
                      "--selection-with-ellipsis", placeholder],
                     profile=tgt_profile, timeout=30)
        except Exception:
            pass
        repositioned += 1

    # 3. 按 block_id 精确删除末尾原始文件块
    if repositioned > 0 and original_view_ids:
        time.sleep(0.3)
        try:
            post_blocks = read_docx_blocks(tgt_doc_id, tgt_profile)
            root_children = [b for b in post_blocks
                             if b.get("parent_id") == tgt_doc_id]
            # 找到原始 block_id 在当前 root_children 中的 index，从后往前删
            indices_to_delete = []
            for idx, b in enumerate(root_children):
                if b["block_id"] in original_view_ids:
                    indices_to_delete.append(idx)
            for idx in reversed(indices_to_delete):
                lark_api("DELETE",
                    f"/open-apis/docx/v1/documents/{tgt_doc_id}/blocks/{tgt_doc_id}"
                    f"/children/batch_delete",
                    data={"start_index": idx, "end_index": idx + 1},
                    profile=tgt_profile)
            if indices_to_delete:
                log(f"  [docx·后处理] 已删除末尾 {len(indices_to_delete)} 个原始文件块")
        except Exception as e:
            log(f"  [docx·后处理] 警告：删除末尾原始文件块失败 — {e}")
    # 由于 insert_after 创建的也是 bt=33，难以精确区分哪些是原始的、哪些是复位的，
    # 暂不自动删除，避免误删。后续可通过记录原始 block_id 来精确清理。

    if repositioned:
        log(f"  [docx·后处理] 已将 {repositioned} 个嵌入文件复位到原位置")


def post_reposition_boards(tgt_doc_id, media_results, tgt_profile):
    """将文档末尾的画板缩略图（图片）复位到占位符位置。

    画板缩略图作为图片上传，无法用 insert_after ![img](token) 复位
    （token 会被忽略），所以画板在 Block API 路径中使用交错写入。
    本函数仅用于 fallback：如果交错写入失败，尝试其他方案。

    当前实现：画板缩略图无法通过后处理复位，保持占位符+末尾现状。
    """
    # 画板缩略图是图片类型，insert_after ![img](token) 创建的图片 token 为空，
    # 所以无法通过后处理复位。保持现状。
    pass

# ============================================================
# 占位符统一（export/import 路径专用）
# ============================================================

def post_migrate_fix_placeholders(tgt_doc_id, src_blocks, state):
    """将 export/import 路径产生的飞书自动文本替换为统一格式的占位符。

    为什么需要这个？
    export/import 后，飞书会将无法保留的嵌入内容替换为特定的中文文本，
    但这些文本格式不统一，也缺少有用信息（如 token）。
    本函数将它们替换为与 Block API 路径一致的统一占位符，
    确保无论走哪条迁移路径，最终文档中的占位符格式都相同。

    替换规则：
    - "点击图片可查看完整电子表格" → "[嵌入电子表格: {token}] (已迁移到文档末尾...)"
    - "点击图片可查看完整表格" → "[嵌入多维表格: {token}] (跨组织不可用...)"
    - "{文件名}" 或 "[{文件名}]" → "[嵌入文件: {name}] (已迁移到文档末尾...)"

    使用 docs +update --mode replace_range 进行文本替换。
    """
    tp = state['meta']['target_profile']

    # 收集源文档中需要处理的嵌入信息
    sheet_tokens = []   # bt=30 电子表格 token 列表
    bitable_tokens = [] # bt=18 多维表格 token 列表
    file_names = []     # bt=23 文件名列表

    for b in src_blocks:
        bt = b.get("block_type")
        if bt == 30:
            token = (b.get("sheet") or b.get("view", {})).get("token", "")
            if token:
                sheet_tokens.append(token)
        elif bt == 18:
            token = b.get("bitable", {}).get("token", "")
            if token:
                bitable_tokens.append(token)
        elif bt == 23:
            name = b.get("file", {}).get("name", "")
            if name:
                file_names.append(name)

    if not sheet_tokens and not bitable_tokens and not file_names:
        return

    replaced = 0

    # 1. 替换电子表格占位文本
    # 注意：如果 post_migrate_embed_sheets 已经用 insert_after+delete_range
    # 处理过这些占位符，这里会找不到，属于正常情况（静默跳过即可）
    for token in sheet_tokens:
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_doc_id,
                      "--mode", "replace_range",
                      "--selection-with-ellipsis", "点击图片可查看完整电子表格",
                      "--markdown", f"[嵌入电子表格: {token}] (已迁移到文档末尾，需手动调整位置)"],
                     profile=tp, timeout=30)
            replaced += 1
        except Exception:
            pass  # 可能已被 post_migrate_embed_sheets 删除，正常
        time.sleep(0.3)

    # 2. 替换多维表格占位文本
    for token in bitable_tokens:
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_doc_id,
                      "--mode", "replace_range",
                      "--selection-with-ellipsis", "点击图片可查看完整表格",
                      "--markdown", f"[嵌入多维表格: {token}] (跨组织不可用，需手动重新嵌入)"],
                     profile=tp, timeout=30)
            replaced += 1
        except Exception as e:
            log(f"    [docx·后处理] 警告：替换「多维表格」导入占位文案失败 — {e}")
        time.sleep(0.3)

    # 3. 替换文件附件占位文本
    # 飞书导出格式不固定：纯文件名 "file.txt" 或方括号 "[file.zip]"
    for name in file_names:
        placeholder = f"[嵌入文件: {name}] (已迁移到文档末尾，需手动调整位置)"

        matched = False
        for pattern in [f"[{name}]", name]:
            if matched:
                break
            try:
                run_lark(["docs", "+update",
                          "--doc", tgt_doc_id,
                          "--mode", "replace_range",
                          "--selection-with-ellipsis", pattern,
                          "--markdown", placeholder],
                         profile=tp, timeout=30)
                replaced += 1
                matched = True
            except Exception:
                pass
            time.sleep(0.3)
        if not matched:
            log(f"    [docx·后处理] 警告：未在正文中匹配到文件「{name}」的导入占位片段，跳过替换")

    if replaced:
        log(f"  [docx·后处理] 已将 {replaced} 处导入占位文案统一为项目约定格式")
