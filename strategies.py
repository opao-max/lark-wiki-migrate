#!/usr/bin/env python3
"""
飞书跨组织知识库迁移 — 各类型迁移策略

按文档类型分发的迁移函数：
- docx: Block API 高保真迁移（~98%），含图片时回退到 export/import
- doc/sheet/bitable: 导出为文件 → 导入到目标组织
- file: 下载 → 上传
- mindnote/slides: 创建占位文档（无 API 支持自动迁移）
- shortcut: 在目标创建快捷方式，指向已迁移的源文档
"""

import json
import time
import os
import shutil
import subprocess

from common import (
    WORK_DIR, log, escape_title, lark_api, run_lark, run_lark_json,
    find_node,
    MigrationError, APIError, DocDeletedError, LarkPermissionError,
)
from block_api import read_docx_blocks, write_blocks_bfs
from media import (
    MediaResult, merge_media_results,
    process_images, process_embedded_files,
    process_embedded_sheets, process_boards,
)
from post_process import post_migrate_embed_sheets, post_migrate_fix_placeholders, post_reposition_files

# ============================================================
# 常量
# ============================================================

EXPORT_IMPORT_TYPES = {
    'doc':     {'export_type': 'doc',     'ext': 'docx', 'import_type': 'docx'},
    'sheet':   {'export_type': 'sheet',   'ext': 'xlsx', 'import_type': 'sheet'},
    'bitable': {'export_type': 'bitable', 'ext': 'xlsx', 'import_type': 'bitable'},
}

PLACEHOLDER_TYPES = {'mindnote', 'slides'}

# ============================================================
# 核心：将文档移入目标知识库
# ============================================================

def move_to_wiki(obj_type, obj_token, parent_wiki_token, target_space_id, profile):
    """将已创建/导入的文档移入目标知识库空间。

    飞书中，新创建的文档默认在"我的空间"，需要显式调用此 API 将其
    移入知识库的树形结构中。API 可能同步返回 wiki_token，也可能返回
    异步 task_id 需要轮询。

    Args:
        obj_type: 文档类型（"docx" / "sheet" / "bitable" / "file" 等）
        obj_token: 目标文档的 token
        parent_wiki_token: 目标知识库中的父节点 token（空串 = 根节点）
        target_space_id: 目标知识库空间 ID
        profile: lark-cli profile 名称

    Returns:
        目标知识库中的 node_token
    """
    body = {"obj_type": obj_type, "obj_token": obj_token}
    if parent_wiki_token:
        body["parent_wiki_token"] = parent_wiki_token

    result = lark_api("POST",
        f"/open-apis/wiki/v2/spaces/{target_space_id}/nodes/move_docs_to_wiki",
        data=body, profile=profile)

    if not result:
        raise MigrationError("move_docs_to_wiki: empty response")
    if result.get("code", -1) != 0:
        raise APIError(f"move_docs_to_wiki: code={result.get('code')}, msg={result.get('msg','')[:200]}")

    rd = result.get("data", {})

    # 同步返回
    wt = rd.get("wiki_token")
    if wt:
        return wt

    # 异步返回：轮询任务
    tid = rd.get("task_id")
    if tid:
        time.sleep(2)
        tr = lark_api("GET", f"/open-apis/wiki/v2/tasks/{tid}",
                       params={"task_type": "move"}, profile=profile)
        if not tr:
            raise MigrationError(f"task {tid}: empty response")
        try:
            return tr["data"]["task"]["move_result"][0]["node"]["node_token"]
        except (KeyError, IndexError, TypeError):
            raise MigrationError(f"task {tid}: bad format: {json.dumps(tr, ensure_ascii=False)[:300]}")

    raise MigrationError(f"move_docs_to_wiki: neither wiki_token nor task_id: {json.dumps(rd, ensure_ascii=False)[:200]}")

# ============================================================
# 核心：解析目标父节点
# ============================================================

def get_target_parent(state, node):
    """根据源节点的父节点，在目标知识库中找到对应的父节点 token。

    查找逻辑：
    1. 源节点无父节点 → 返回配置的 target_root_node_token（挂在根下）
    2. 父节点已迁移（在 node_mapping 中有记录）→ 返回映射后的 token
    3. 父节点在 state 中但未迁移 → 报错（迁移顺序有误）
    4. 父节点不在 state 中 → 可能是跨空间节点，挂在根下并告警
    """
    psrc = node.get("parent_node_token", "")
    if not psrc:
        return state['meta'].get('target_root_node_token', "")
    ptgt = state['node_mapping'].get(psrc)
    if ptgt:
        return ptgt
    parent_in_state = any(n['node_token'] == psrc for n in state['nodes'])
    if parent_in_state:
        raise MigrationError(f"parent {psrc} not yet migrated (no mapping)")
    log(f"    WARN: parent {psrc[:20]} not in state, placing under wiki root")
    return ""

# ============================================================
# 导入结果 token 提取
# ============================================================

def _extract_import_token(result, profile):
    """从 lark-cli drive +import 的结果中提取目标文档 token。"""
    if not result:
        return None
    data = result.get("data", {})
    if not isinstance(data, dict):
        return None

    tk = data.get("token") or data.get("file_token")
    if tk:
        return tk

    ticket = data.get("ticket")
    if ticket:
        return _poll_import_ticket(ticket, profile)

    return None


def _poll_import_ticket(ticket, profile):
    """轮询异步导入任务直到完成（最多约 60 秒）"""
    for _ in range(30):
        time.sleep(2)
        r = lark_api("GET", f"/open-apis/drive/v1/import_tasks/{ticket}", profile=profile)
        if not r:
            continue
        res = r.get("data", {}).get("result", {})
        status = res.get("job_status", 0)
        if status == 1 or status == "success":
            return res.get("token")
        if status == 2 or status == "failed":
            raise MigrationError(f"import failed: ticket={ticket}")
    raise MigrationError(f"import timed out: ticket={ticket}")

# ============================================================
# docx 迁移策略（Block API 高保真迁移）
# ============================================================

def migrate_docx(node, state):
    """docx 文档迁移主流程 —— 整个项目最核心的函数。

    飞书 docx 由 Block 树组成（类似 DOM），本函数选择最优策略将其完整搬运到目标组织。

    有两条迁移路径：
      A. Block API 路径（优先）：逐个读取源 blocks → 迁移媒体资源 → 逐层写入目标
         保真度 ~98%，保留所有富文本格式、表格、分栏布局等。
         限制：飞书 Block API 不支持创建图片 block (bt=27)。

      B. Export/Import 路径（回退）：整篇导出为 .docx 文件 → 导入到目标
         能保留图片，但丢失部分格式。含图片时自动选此路径。
         写入失败时也会降级到此路径。

    完整流程（Block API 路径）：
      1. read_docx_blocks()          → 分页读取源文档所有 blocks   [block_api.py]
      2. 检测图片 → 有图片则走 export/import 回退
      3. 创建目标空白 docx
      4. _migrate_media_resources()  → 迁移媒体资源               [media.py]
         返回 {block_id: MediaResult}，不修改 blocks（图片除外）
      5. _preprocess_blocks_for_write() → 处理文件 block 的父子关系
      6. write_blocks_bfs()          → BFS 逐层写入 blocks        [block_api.py]
         clean_block() 接收 MediaResult 决定每个 block 的输出形式
      7. post_migrate_embed_sheets() → 嵌入表格数据复制            [post_process.py]
      8. move_to_wiki()              → 挂入目标知识库

    Args:
        node: 待迁移节点信息 dict，包含 obj_token, title, parent_node_token 等
        state: 全局迁移状态，包含 meta（profile/space 配置）、node_mapping 等

    Returns:
        (tgt_obj_token, tgt_node_token) 元组
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    title = escape_title(node['title'])
    obj_token = node['obj_token']

    # ---- 步骤 1: 读取源文档的 Block 树 ----
    # 飞书 docx 的结构：block_type=1 是页面根节点（Page），其余 block 以树形挂在下面。
    # read_docx_blocks 自动处理分页（每页最多 500 个 block）。
    log(f"  [docx] 正在读取源文档块结构（Block API），document_id={obj_token}")
    blocks = read_docx_blocks(obj_token, sp)
    if not blocks:
        raise MigrationError(f"no blocks in {obj_token}")

    # 找到根节点，后续 write_blocks_bfs 需要从根开始重建树
    page_block = next((b for b in blocks if b.get("block_type") == 1), None)
    if not page_block:
        raise MigrationError(f"no page block in {obj_token}")
    src_page_id = page_block["block_id"]

    # ---- 步骤 2: 图片检测 → 决定迁移路径 ----
    # 飞书 Block API 不支持创建 image block (bt=27)，
    # 所以含图片的文档必须走 export/import 回退路径来保留图片。
    img_count = sum(1 for b in blocks if b.get("block_type") == 27)
    if img_count > 0:
        log(f"  [docx] 检测到 {img_count} 个图片块；Block API 无法新建图片，改为导出/导入整篇以保留图片。")
        return _migrate_docx_export_import(node, state)

    # ---- 步骤 3: 创建目标空白 docx ----
    log(f"  [docx] 正在目标端创建空白文档「{title[:60]}」")
    cr = lark_api("POST", "/open-apis/docx/v1/documents",
                  data={"title": title}, profile=tp)
    if not cr or cr.get("code", -1) != 0:
        raise MigrationError(f"create docx: {json.dumps(cr or {}, ensure_ascii=False)[:200]}")
    tgt_doc_id = cr["data"]["document"]["document_id"]

    # ---- 步骤 4: 迁移媒体资源 ----
    # 将文档中嵌入的文件/表格/画板等下载后上传到目标组织。
    # 返回 {block_id: MediaResult} 字典，后续 clean_block 通过查询此字典
    # 决定每个 block 应该生成什么内容（占位符 or 正常写入）。
    # 关键设计：不修改 blocks 本身（图片除外），通过显式数据结构通信。
    log("  [docx] 正在迁移文档内资源：图片、附件、嵌入表格、画板…")
    media_results = _migrate_media_resources(blocks, tgt_doc_id, state)

    # ---- 步骤 5: 预处理 blocks ----
    # 处理文件 block (bt=23) 与父容器的关系：
    # - bt=23 在 bt=33 (view容器) 下 → 标记为 handled_by_parent，由父级统一生成占位符
    # - bt=23 在 bt=2 (文本段落) 下 → 替换 inline_block 元素为占位文本
    # 这一步修改 media_results 而不是 blocks，保持数据流清晰。
    _preprocess_blocks_for_write(blocks, media_results)

    # ---- 步骤 6: BFS 逐层写入 ----
    # 飞书 Block API 要求：先创建父 block，才能在其下创建子 block。
    # 所以按 BFS（广度优先）顺序逐层写入，每批最多 50 个。
    # 批量写入失败时，自动降级为逐个写入以定位问题 block。
    # 如果整体写入失败，降级到 export/import 回退路径。
    try:
        log(f"  [docx] 正在通过 Block API 写入块（源树共 {len(blocks)} 个 block）…")
        cnt = write_blocks_bfs(blocks, tgt_doc_id, src_page_id, tp, media_results)
        log(f"  [docx] Block API 写入结束：本次 API 成功创建 {cnt} 个块，目标 document_id={tgt_doc_id}")
    except (APIError, MigrationError) as e:
        log(f"  [docx] Block API 整体写入失败：{str(e)[:300]}")
        log("  [docx] 将删除未完成的目标文档，并改用「导出 → 导入」回退路径。")
        # 写入失败 → 删除已创建的空文档 → 走 export/import
        try:
            lark_api("DELETE", f"/open-apis/drive/v1/files/{tgt_doc_id}",
                     data={"type": "docx"}, profile=tp)
        except Exception:
            pass
        return _migrate_docx_export_import(node, state)

    # ---- 步骤 7: 后处理 — 嵌入表格数据复制 ----
    # 嵌入的电子表格已经在步骤 4 中通过 export/import 创建了目标 sheet，
    # 这里将 <sheet/> 标签插入占位符位置，并从源表格读取数据写入目标表格。
    post_migrate_embed_sheets(tgt_doc_id, media_results, state)

    # （Block API 路径中，根级文件和画板通过 BFS 交错写入已在原位）
    # 非根级文件（inline_block 等）需要单独上传+后处理复位
    _upload_remaining_files(tgt_doc_id, media_results, tp)
    post_reposition_files(tgt_doc_id, media_results, tp)

    # ---- 步骤 8: 挂入目标知识库 ----
    # 文档创建后默认在"我的空间"，需要调用 move_docs_to_wiki 移入知识库。
    parent = get_target_parent(state, node)
    node_tk = move_to_wiki("docx", tgt_doc_id, parent, tsi, tp)
    if not node_tk:
        raise MigrationError("move_to_wiki returned empty node_token!")
    return tgt_doc_id, node_tk


def _migrate_media_resources(blocks, tgt_doc_id, state):
    """迁移文档中所有媒体资源，返回合并后的 {block_id: MediaResult} 字典。

    四种媒体资源各自独立处理，最后合并为一个字典：
    - 图片 (bt=27): 下载 → 上传 → 替换 block 中的 token（唯一会修改 blocks 的操作）
    - 嵌入文件 (bt=23): 下载 → 上传到文档末尾
    - 嵌入表格 (bt=30): export xlsx → import 到目标 → 记录 token 映射
    - 画板 (bt=43): 下载缩略图 → 上传为图片

    返回的字典会传递给 clean_block() 和 post_process，
    是 media.py → block_api.py 数据流的核心载体。
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']

    img_results = process_images(blocks, tgt_doc_id, sp, tp)
    file_results = process_embedded_files(blocks, tgt_doc_id, sp, tp, upload=False)
    sheet_results = process_embedded_sheets(blocks, state, sp, tp)
    board_results = process_boards(blocks, tgt_doc_id, sp, tp)

    return merge_media_results(img_results, file_results, sheet_results, board_results)


def _upload_remaining_files(tgt_doc_id, media_results, tgt_profile):
    """上传未被交错写入处理的文件（inline_block 等非根级文件）。

    交错写入处理了根级 bt=33 容器下的文件，
    但 inline_block (pbt=2) 文件未被处理，local_path 仍存在。
    这里将它们上传到文档末尾，后续由 post_reposition_files 复位。
    """
    for mr in media_results.values():
        if mr.resource_type != "file" or not mr.success or not mr.local_path:
            continue
        # local_path 仍存在说明没有被交错写入消费
        if not os.path.exists(mr.local_path):
            continue
        try:
            fname = os.path.basename(mr.local_path)
            work_dir = os.path.dirname(mr.local_path)
            result = subprocess.run(
                ["lark-cli", "docs", "+media-insert",
                 "--doc", tgt_doc_id, "--file", f"./{fname}", "--type", "file",
                 "--profile", tgt_profile, "--as", "user"],
                capture_output=True, text=True, timeout=180, cwd=work_dir)
            if result.returncode == 0:
                try:
                    up_json = json.loads(result.stdout[result.stdout.find('{'):])
                    mr.target_token = up_json.get("data", {}).get("file_token", "")
                except Exception:
                    pass
                log(f"    [docx·资源] 嵌入文件「{mr.name}」已上传（非根级，待后处理复位）")
            else:
                mr.success = False
                log(f"    [docx·资源] 警告：嵌入文件「{mr.name}」上传失败")
        except Exception as e:
            mr.success = False
            log(f"    [docx·资源] 警告：嵌入文件「{mr.name}」上传异常 — {e}")
        finally:
            try:
                os.remove(mr.local_path)
            except Exception:
                pass


def _preprocess_blocks_for_write(blocks, media_results):
    """在 BFS 写入前，处理文件 block (bt=23) 与父容器的关系。

    飞书文档中，文件附件 (bt=23) 可能出现在三种位置：
    1. 直接挂在页面根节点下 → 正常处理，clean_block 生成占位符
    2. 在 view 容器 (bt=33) 下 → 父级统一生成占位符，子级标记跳过
    3. 在文本段落 (bt=2) 中作为 inline_block → 替换为占位文本

    本函数处理后两种情况：
    - 情况 2: 设置 mr.handled_by_parent = True，
      并为 bt=33 父级创建 MediaResult(resource_type="file_container")
    - 情况 3: 将 inline_block 元素替换为占位文本
      （因为 Block API 不支持写入 inline_block，会报错 1770024）

    注意：本函数修改的是 media_results 字典（添加新条目、设置 flag），
    以及 bt=2 父 block 的 elements 数组，而不是创建新的数据结构。
    """
    block_map = {b["block_id"]: b for b in blocks}

    for b in blocks:
        if b.get("block_type") != 23:
            continue
        mr = media_results.get(b["block_id"])
        if not mr:
            continue

        parent = block_map.get(b.get("parent_id"))
        if not parent:
            continue

        name = b.get("file", {}).get("name", "未知文件")
        pbt = parent.get("block_type")

        if pbt == 33:
            # view 容器包含文件 → 标记为由父级处理
            mr.handled_by_parent = True
            # 在 media_results 中为父级 bt=33 创建容器记录
            parent_id = parent["block_id"]
            if parent_id not in media_results:
                media_results[parent_id] = MediaResult(
                    parent_id, "file_container", "", success=True)
            media_results[parent_id].child_files.append({
                "name": name, "success": mr.success})

        elif pbt == 2:
            # 文本 block 内嵌文件（inline_block）→ 替换 inline_block 元素
            # Block API 不支持写入 inline_block，会报 1770024
            mr.handled_by_parent = True
            text_content = parent.get("text", {})
            elements = text_content.get("elements", [])
            new_elements = []
            for elem in elements:
                if "inline_block" in elem:
                    if mr.success:
                        placeholder = f"[嵌入文件: {name}] (已迁移到文档末尾，需手动调整位置)"
                    else:
                        placeholder = f"[嵌入文件: {name}] (迁移失败)"
                    new_elements.append({"text_run": {"content": placeholder, "text_element_style": {}}})
                else:
                    new_elements.append(elem)
            text_content["elements"] = new_elements

# ============================================================
# docx export/import 回退路径
# ============================================================

def _migrate_docx_export_import(node, state):
    """docx 回退方案：通过 export → import 文件级迁移。

    当文档含有图片 (bt=27) 或 Block API 写入失败时，走此路径。
    优点：能完整保留图片。
    缺点：丢失部分富文本格式（如 callout 样式、分栏布局等）。

    流程：
    1. 导出源文档为 .docx 文件
    2. 导入到目标组织
    3. 后处理 — 嵌入表格：读取源 blocks 找到 bt=30，export/import sheet 后插入文档
    4. 后处理 — 占位符统一：飞书导入后的自动文本（如"点击图片可查看完整电子表格"）
       替换为与 Block API 路径一致的统一格式
    5. 后处理 — 嵌入文件：下载上传文件附件（必须在占位符替换之后，避免文件名匹配冲突）
    6. 挂入知识库

    两条路径（Block API / export-import）的后处理产出格式完全一致，
    确保最终文档中的占位符样式统一。
    """
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    obj_token = node['obj_token']
    title = escape_title(node['title'])

    node_dir = f"node_{node['node_token']}"
    node_dir_abs = os.path.join(WORK_DIR, node_dir)
    os.makedirs(node_dir_abs, exist_ok=True)

    try:
        # ---- 导出源文档为 .docx 文件 ----
        log(f"  [docx·回退] 正在从源组织导出 docx 文件… document_id={obj_token}")
        run_lark(["drive", "+export",
                  "--token", obj_token,
                  "--doc-type", "docx",
                  "--file-extension", "docx",
                  "--output-dir", f"./{node_dir}",
                  "--overwrite"],
                 profile=sp, cwd=WORK_DIR, timeout=180)

        files = [f for f in os.listdir(node_dir_abs)
                 if os.path.isfile(os.path.join(node_dir_abs, f)) and not f.startswith('.')]
        if not files:
            raise MigrationError(f"export produced no file for {obj_token}")

        # ---- 导入到目标组织 ----
        log(f"  [docx·回退] 正在向目标组织导入为新版文档「{title[:60]}」…")
        imp = run_lark_json(
            ["drive", "+import",
             "--file", f"./{node_dir}/{files[0]}",
             "--type", "docx",
             "--name", title],
            profile=tp, cwd=WORK_DIR, timeout=180)

        tgt_token = _extract_import_token(imp, tp)
        if not tgt_token:
            raise MigrationError(f"import returned no token: {json.dumps(imp, ensure_ascii=False)[:200]}")
        log(f"  [docx·回退] 导入完成，目标 document_id={tgt_token}")

        # ---- 后处理准备：读取源 blocks ----
        # export/import 后需要读源 blocks 来获取嵌入内容的元信息
        # （类型、token、文件名等），以便后续处理
        src_blocks = []
        try:
            src_blocks = read_docx_blocks(obj_token, sp)
        except Exception:
            pass

        # ---- 后处理 1: 嵌入表格 ----
        # 找到源文档中的 bt=30 (嵌入电子表格)，导出 xlsx 后导入目标，
        # 再用 <sheet/> 标签插入文档末尾并复制数据
        if src_blocks:
            try:
                has_sheets = any(b.get("block_type") == 30 for b in src_blocks)
                if has_sheets:
                    log("  [docx·回退] 后处理：嵌入电子表格（导出/导入表 + 写入数据）…")
                    sheet_results = process_embedded_sheets(src_blocks, state, sp, tp)
                    post_migrate_embed_sheets(tgt_token, sheet_results, state)
            except Exception as e:
                log(f"  [docx·回退] 警告：嵌入表格后处理失败：{e}")

        # ---- 后处理 2: 占位符统一 ----
        # 飞书导入后会将无法保留的嵌入内容替换为特定文本：
        #   电子表格 → "点击图片可查看完整电子表格"
        #   多维表格 → "点击图片可查看完整表格"
        #   文件附件 → 纯文件名文本
        # 这里将这些文本替换为与 Block API 路径一致的统一占位符格式。
        # 必须在文件上传之前执行，否则文件名可能产生误匹配。
        if src_blocks:
            try:
                post_migrate_fix_placeholders(tgt_token, src_blocks, state)
            except Exception as e:
                log(f"  WARN: post-import placeholder fix failed: {e}")

        # ---- 后处理 3: 嵌入文件上传 + 位置复位 ----
        # 在占位符替换之后执行，避免文件名与占位符文本重复匹配
        if src_blocks:
            try:
                has_files = any(b.get("block_type") == 23 for b in src_blocks)
                if has_files:
                    log(f"  [post-import] processing embedded files...")
                    file_results = process_embedded_files(src_blocks, tgt_token, sp, tp)
                    # 将末尾文件复位到占位符位置
                    post_reposition_files(tgt_token, file_results, tp)
            except Exception as e:
                log(f"  WARN: post-import embed file processing failed: {e}")

        # 挂入知识库
        parent = get_target_parent(state, node)
        node_tk = move_to_wiki("docx", tgt_token, parent, tsi, tp)
        if not node_tk:
            raise MigrationError("move_to_wiki returned empty node_token!")
        return tgt_token, node_tk

    finally:
        shutil.rmtree(node_dir_abs, ignore_errors=True)

# ============================================================
# doc/sheet/bitable（导出 → 导入）
# ============================================================

def migrate_export_import(node, state):
    """通过文件导出导入迁移 doc/sheet/bitable。"""
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    obj_token = node['obj_token']
    obj_type = node['obj_type']
    title = escape_title(node['title'])

    cfg = EXPORT_IMPORT_TYPES[obj_type]
    node_dir = f"node_{node['node_token']}"
    node_dir_abs = os.path.join(WORK_DIR, node_dir)
    os.makedirs(node_dir_abs, exist_ok=True)

    try:
        log(f"  [export] {obj_type}: {obj_token}")
        run_lark(["drive", "+export",
                  "--token", obj_token,
                  "--doc-type", cfg['export_type'],
                  "--file-extension", cfg['ext'],
                  "--output-dir", f"./{node_dir}",
                  "--overwrite"],
                 profile=sp, cwd=WORK_DIR, timeout=180)

        files = [f for f in os.listdir(node_dir_abs)
                 if os.path.isfile(os.path.join(node_dir_abs, f)) and not f.startswith('.')]
        if not files:
            raise MigrationError(f"export produced no file for {obj_token}")
        log(f"  exported: {files[0]}")

        log(f"  [import] as {cfg['import_type']}: {title[:60]}")
        imp = run_lark_json(
            ["drive", "+import",
             "--file", f"./{node_dir}/{files[0]}",
             "--type", cfg['import_type'],
             "--name", title],
            profile=tp, cwd=WORK_DIR, timeout=180)

        tgt_token = _extract_import_token(imp, tp)
        if not tgt_token:
            raise MigrationError(f"import returned no token: {json.dumps(imp, ensure_ascii=False)[:200]}")
        log(f"  imported: {tgt_token}")

        parent = get_target_parent(state, node)
        node_tk = move_to_wiki(cfg['import_type'], tgt_token, parent, tsi, tp)
        if not node_tk:
            raise MigrationError("move_to_wiki returned empty node_token!")
        return tgt_token, node_tk

    finally:
        shutil.rmtree(node_dir_abs, ignore_errors=True)

# ============================================================
# file（下载 → 上传）
# ============================================================

def migrate_file(node, state):
    """迁移文件类型节点：从源下载 → 上传到目标 → 移入知识库"""
    sp = state['meta']['source_profile']
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    obj_token = node['obj_token']
    title = escape_title(node['title'])

    dl_name = f"dl_{node['node_token']}"
    dl_abs = os.path.join(WORK_DIR, dl_name)

    upload_abs = dl_abs
    try:
        if os.path.exists(dl_abs):
            os.remove(dl_abs)

        log(f"  [download] file: {obj_token}")
        run_lark(["drive", "+download",
                  "--file-token", obj_token,
                  "--output", f"./{dl_name}",
                  "--overwrite"],
                 profile=sp, cwd=WORK_DIR, timeout=180)

        if not os.path.isfile(dl_abs):
            raise MigrationError(f"download produced no file for {obj_token}")
        log(f"  downloaded: {dl_name} ({os.path.getsize(dl_abs)} bytes)")

        # 重命名为原始文件名，确保飞书能正确识别文件类型（扩展名）
        upload_name = title if '.' in title else dl_name
        upload_abs = os.path.join(WORK_DIR, upload_name)
        try:
            if upload_abs != dl_abs:
                shutil.copy2(dl_abs, upload_abs)
        except Exception:
            upload_abs = dl_abs
            upload_name = dl_name

        log(f"  [upload] {title[:60]}")
        up = run_lark_json(
            ["drive", "+upload",
             "--file", f"./{upload_name}"],
            profile=tp, cwd=WORK_DIR, timeout=180)

        tgt_token = (up or {}).get("data", {}).get("file_token")
        if not tgt_token:
            raise MigrationError(f"upload returned no token: {json.dumps(up, ensure_ascii=False)[:200]}")
        log(f"  uploaded: {tgt_token}")

        parent = get_target_parent(state, node)
        node_tk = move_to_wiki("file", tgt_token, parent, tsi, tp)
        if not node_tk:
            raise MigrationError("move_to_wiki returned empty node_token!")
        return tgt_token, node_tk

    finally:
        if os.path.exists(dl_abs):
            os.remove(dl_abs)
        if upload_abs != dl_abs and os.path.exists(upload_abs):
            os.remove(upload_abs)

# ============================================================
# mindnote/slides（占位文档）
# ============================================================

def migrate_placeholder(node, state):
    """为 mindnote/slides 创建占位 docx 文档。"""
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    obj_type = node['obj_type']
    title = escape_title(node['title'])
    ph_title = f"[手动迁移] {title}"

    create_data = {
        "node_type": "origin",
        "obj_type": "docx",
        "title": ph_title,
    }
    psrc = node.get("parent_node_token", "")
    if psrc:
        ptgt = state['node_mapping'].get(psrc)
        if ptgt:
            create_data["parent_node_token"] = ptgt
    else:
        root_parent = state['meta'].get('target_root_node_token', "")
        if root_parent:
            create_data["parent_node_token"] = root_parent

    log(f"  [placeholder] {obj_type}: {ph_title[:60]}")
    r = run_lark_json(
        ["wiki", "nodes", "create",
         "--params", json.dumps({"space_id": tsi}),
         "--data", json.dumps(create_data, ensure_ascii=False)],
        profile=tp)

    if not r or r.get("code", -1) != 0:
        raise MigrationError(f"create placeholder: {json.dumps(r or {}, ensure_ascii=False)[:200]}")

    nd = r.get("data", {}).get("node", {})
    tgt_node_tk = nd.get("node_token")
    tgt_obj_tk = nd.get("obj_token")

    if not tgt_node_tk:
        raise MigrationError("placeholder: no node_token returned")

    # 写入占位内容
    sp = state['meta']['source_profile']
    source_url = f"https://{sp}.feishu.cn/wiki/{node['node_token']}"
    md = (
        f"# [需手动迁移] {title}\n\n"
        f"**类型**: {obj_type}\n\n"
        f"**源链接**: {source_url}\n\n"
        f"---\n\n"
        f"此文档为占位。原 {obj_type} 暂不支持跨组织 API 迁移，请从源组织手动导出后上传。\n"
    )
    if tgt_obj_tk:
        try:
            run_lark(["docs", "+update",
                      "--doc", tgt_obj_tk,
                      "--mode", "overwrite",
                      "--markdown", md],
                     profile=tp, timeout=30)
        except Exception as e:
            log(f"    WARN: write placeholder content failed: {e}")

    return tgt_obj_tk, tgt_node_tk

# ============================================================
# 快捷方式 (shortcut)
# ============================================================

def _is_broken_shortcut(node):
    """判断快捷方式是否已失效"""
    origin = node.get("origin_node_token", "")
    origin_space = node.get("origin_space_id", "")
    return (not origin or origin == "null" or
            origin_space == "0" or origin_space == "")


def migrate_shortcut_as_doc(node, state):
    """将失效的快捷方式当作普通文档迁移。"""
    obj_type = node['obj_type']
    title = escape_title(node['title'])

    log(f"  [shortcut→doc] broken shortcut, migrating as {obj_type}: {title[:60]}")

    doc_node = dict(node)
    doc_node['node_type'] = 'origin'

    if obj_type == 'docx':
        return migrate_docx(doc_node, state)
    elif obj_type in EXPORT_IMPORT_TYPES:
        return migrate_export_import(doc_node, state)
    elif obj_type == 'file':
        return migrate_file(doc_node, state)
    else:
        return migrate_placeholder(doc_node, state)


def migrate_shortcut(node, state):
    """迁移快捷方式。失效的快捷方式自动降级为普通文档迁移。"""
    tp = state['meta']['target_profile']
    tsi = state['meta']['target_space_id']
    title = escape_title(node['title'])

    if _is_broken_shortcut(node):
        log(f"  [shortcut] {title[:60]}: origin broken, migrating as document")
        return migrate_shortcut_as_doc(node, state)

    origin_src = node.get("origin_node_token")

    tgt_origin = state['node_mapping'].get(origin_src)
    if not tgt_origin:
        log(f"  [shortcut] {title[:60]}: origin {origin_src} not in this space, migrating as document")
        return migrate_shortcut_as_doc(node, state)

    psrc = node.get("parent_node_token", "")
    if psrc:
        ptgt = state['node_mapping'].get(psrc, "")
    else:
        ptgt = state['meta'].get('target_root_node_token', "")
    if psrc and not ptgt:
        raise MigrationError(f"parent {psrc} not migrated")

    origin_node = find_node(state, origin_src)
    obj_type = origin_node['obj_type'] if origin_node else node.get('obj_type', 'docx')
    if obj_type == 'shortcut':
        obj_type = 'docx'

    create_data = {
        "node_type": "shortcut",
        "obj_type": obj_type,
        "origin_node_token": tgt_origin,
    }
    if ptgt:
        create_data["parent_node_token"] = ptgt

    log(f"  [shortcut] {title[:60]} → origin={tgt_origin}")
    r = run_lark_json(
        ["wiki", "nodes", "create",
         "--params", json.dumps({"space_id": tsi}),
         "--data", json.dumps(create_data, ensure_ascii=False)],
        profile=tp)

    if not r or r.get("code", -1) != 0:
        raise MigrationError(f"create shortcut: {json.dumps(r or {}, ensure_ascii=False)[:300]}")

    nd = r.get("data", {}).get("node", {})
    return nd.get("obj_token"), nd.get("node_token")

# ============================================================
# 迁移分发器
# ============================================================

def migrate_node(node, state):
    """根据 obj_type 分发到对应的迁移函数"""
    t = node['obj_type']
    if t == 'docx':
        return migrate_docx(node, state)
    elif t in EXPORT_IMPORT_TYPES:
        return migrate_export_import(node, state)
    elif t == 'file':
        return migrate_file(node, state)
    elif t in PLACEHOLDER_TYPES:
        return migrate_placeholder(node, state)
    else:
        raise MigrationError(f"unsupported type: {t}")