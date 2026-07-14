#!/usr/bin/env python3
"""
飞书跨组织知识库迁移 — 媒体资源迁移

负责文档中嵌入的图片、文件附件、电子表格、画板的跨组织迁移（下载 → 上传）。
每个 process_* 函数返回 {block_id: MediaResult}，不修改原始 blocks，
调用方通过 MediaResult 决定后续如何处理（生成占位符、复制数据等）。

数据流：
    strategies.py 调用 process_* → 得到 media_results (dict)
    strategies.py 传 media_results 给 block_api.clean_block → 生成正确的占位符
    strategies.py 传 media_results 给 post_process → 复制 sheet 数据
"""

import json
import time
import os
import shutil
import subprocess
from dataclasses import dataclass, field

from common import (
    WORK_DIR, log, lark_api, run_lark, run_lark_json,
    MigrationError,
)

# ============================================================
# 媒体迁移结果
# ============================================================

@dataclass
class MediaResult:
    """单个媒体资源的迁移结果 —— 消除隐式 flag 通信的核心数据结构。

    旧设计中，process_* 函数会在 block 上挂 _file_inserted=True 等下划线 flag，
    然后 clean_block 读取这些 flag 决定生成什么占位符。
    读者必须在两个文件之间来回跳才能理解数据流。

    新设计中，process_* 函数返回 {block_id: MediaResult} 字典，
    clean_block 通过参数接收这个字典，实现单向数据流：
        media.py (产生) → MediaResult → block_api.py (消费)

    每个 MediaResult 描述"某个 block 对应的媒体资源迁移得怎么样了"：
    - success=True: 迁移成功，target_token 是目标 token
    - success=False: 迁移失败，clean_block 会生成失败占位符
    - handled_by_parent=True: 此 block 已由父级处理，clean_block 返回 None 跳过
    """
    block_id: str               # 源 block_id（用作字典的 key）
    resource_type: str          # 资源类型："image" | "file" | "sheet" | "board" | "file_container"
    name: str                   # 显示名称（文件名 / token，用于占位符文本）
    success: bool               # 是否迁移成功
    target_token: str = ""      # 目标 token（成功时有值）
    src_token: str = ""         # 源 token（嵌入表格数据复制时需要）
    handled_by_parent: bool = False  # bt=23 被父级 bt=33/bt=2 处理时为 True
    child_files: list = field(default_factory=list)  # bt=33 容器的子文件信息 [{"name", "success"}]
    local_path: str = ""        # 画板缩略图本地文件路径（交错写入时使用）


def merge_media_results(*dicts):
    """合并多个 media_results 字典。"""
    merged = {}
    for d in dicts:
        merged.update(d)
    return merged

# ============================================================
# 图片 (block_type=27)
# ============================================================

def process_images(blocks, target_doc_id, src_profile, tgt_profile):
    """下载源文档中的图片，上传到目标文档，返回迁移结果。

    流程（对每个 bt=27 图片 block）：
    1. docs +media-download 从源文档下载图片到临时文件
    2. docs +media-insert 上传到目标文档（会在文档末尾创建临时 block）
    3. 提取新 file_token
    4. 删除 media-insert 创建的临时 block（图片会由 write_blocks_bfs 正式写入）
    5. 就地更新 block 中的 image.token 为新值

    注意：这是唯一会修改 blocks 的 process_* 函数。
    原因：write_blocks_bfs 创建图片 block 时需要使用目标组织的 file_token，
    所以必须在写入前将 token 更新为上传后的新值。

    Returns:
        {block_id: MediaResult}
    """
    results = {}

    for block in blocks:
        img = block.get("image")
        if not img or "token" not in img:
            continue

        block_id = block["block_id"]
        old_tk = img["token"]
        dl_name = f"img_{old_tk}"
        dl_abs = os.path.join(WORK_DIR, dl_name)

        try:
            import glob as _glob
            for f in _glob.glob(dl_abs + "*"):
                os.remove(f)

            # 从源文档下载图片
            dl_cmd = ["lark-cli", "docs", "+media-download",
                      "--token", old_tk,
                      "--output", f"./{dl_name}",
                      "--profile", src_profile, "--as", "user"]
            dl_r = subprocess.run(dl_cmd, capture_output=True, text=True,
                                  timeout=120, cwd=WORK_DIR)
            if dl_r.returncode != 0:
                log(f"    [docx·资源] 警告：图片下载失败（media-download）token={old_tk}")
                results[block_id] = MediaResult(block_id, "image", old_tk, success=False)
                continue

            # media-download 可能自动追加扩展名
            candidates = _glob.glob(dl_abs + "*")
            if not candidates:
                log(f"    [docx·资源] 警告：图片未下载到本地文件 token={old_tk}")
                results[block_id] = MediaResult(block_id, "image", old_tk, success=False)
                continue

            actual_path = candidates[0]
            actual_name = os.path.basename(actual_path)

            # 上传到目标文档
            upload_cmd = ["lark-cli", "docs", "+media-insert",
                          "--doc", target_doc_id,
                          "--file", f"./{actual_name}",
                          "--type", "image",
                          "--profile", tgt_profile, "--as", "user"]
            up_r = subprocess.run(upload_cmd, capture_output=True, text=True,
                                  timeout=120, cwd=WORK_DIR)
            if up_r.returncode != 0:
                log(f"    [docx·资源] 警告：图片上传到目标文档失败（media-insert）token={old_tk}")
                results[block_id] = MediaResult(block_id, "image", old_tk, success=False)
                continue

            # 从输出中提取 file_token 和临时 block_id
            try:
                up_json = json.loads(up_r.stdout[up_r.stdout.find('{'):])
                new_tk = up_json.get("data", {}).get("file_token")
                temp_block_id = up_json.get("data", {}).get("block_id")
            except Exception:
                new_tk = None
                temp_block_id = None

            if new_tk:
                img["token"] = new_tk  # 就地更新，write_blocks_bfs 需要新 token
                log(f"    [docx·资源] 图片 token 已替换：{old_tk} → {new_tk}")
                # 删除 media-insert 创建的临时 block
                if temp_block_id:
                    try:
                        subprocess.run([
                            "lark-cli", "api", "DELETE",
                            f"/open-apis/docx/v1/documents/{target_doc_id}/blocks/{temp_block_id}",
                            "--profile", tgt_profile, "--as", "user"
                        ], capture_output=True, text=True, timeout=15, cwd=WORK_DIR)
                    except Exception:
                        pass
                results[block_id] = MediaResult(block_id, "image", old_tk, success=True,
                                                target_token=new_tk)
            else:
                log(f"    [docx·资源] 警告：图片上传未返回 file_token token={old_tk}")
                results[block_id] = MediaResult(block_id, "image", old_tk, success=False)

        except Exception as e:
            log(f"    [docx·资源] 警告：图片处理异常 token={old_tk} — {e}")
            results[block_id] = MediaResult(block_id, "image", old_tk, success=False)
        finally:
            import glob as _glob
            for f in _glob.glob(dl_abs + "*"):
                os.remove(f)

    return results

# ============================================================
# 嵌入文件附件 (block_type=23)
# ============================================================

def process_embedded_files(blocks, target_doc_id, src_profile, tgt_profile, upload=True):
    """下载源文档中的嵌入文件附件 (bt=23)，可选上传到目标文档。

    飞书文档可以嵌入各种文件（PDF、ZIP、图片文件等），
    跨组织时 file token 不可用，需要下载后重新上传。

    下载策略（两级回退）：
    1. drive +download（标准云空间下载）
    2. docs +media-download（文档媒体下载，部分老格式文件需要）

    Args:
        upload: True = 下载+上传（export/import 路径），
                False = 只下载，保留本地路径（Block API 路径，由 BFS 交错写入）

    Returns:
        {block_id: MediaResult}
    """
    results = {}

    for block in blocks:
        if block.get("block_type") != 23:
            continue
        file_info = block.get("file")
        if not file_info or "token" not in file_info:
            results[block["block_id"]] = MediaResult(
                block["block_id"], "file", "未知文件", success=False)
            continue

        block_id = block["block_id"]
        old_tk = file_info["token"]
        file_name = file_info.get("name", "file")
        dl_name = f"embed_{old_tk}"
        dl_abs = os.path.join(WORK_DIR, dl_name)
        renamed_path = None

        try:
            import glob as _glob
            for f in _glob.glob(dl_abs + "*"):
                os.remove(f)

            # 尝试 drive +download
            dl_cmd = ["lark-cli", "drive", "+download",
                      "--file-token", old_tk,
                      "--output", f"./{dl_name}",
                      "--overwrite",
                      "--profile", src_profile, "--as", "user"]
            dl_r = subprocess.run(dl_cmd, capture_output=True, text=True,
                                  timeout=180, cwd=WORK_DIR)
            if dl_r.returncode != 0:
                # 回退：尝试 media-download
                dl_cmd2 = ["lark-cli", "docs", "+media-download",
                           "--token", old_tk,
                           "--output", f"./{dl_name}",
                           "--profile", src_profile, "--as", "user"]
                dl_r = subprocess.run(dl_cmd2, capture_output=True, text=True,
                                      timeout=180, cwd=WORK_DIR)
                if dl_r.returncode != 0:
                    log(f"    [docx·资源] 警告：嵌入文件下载失败「{file_name}」token={old_tk}")
                    results[block_id] = MediaResult(block_id, "file", file_name, success=False)
                    continue

            candidates = _glob.glob(dl_abs + "*")
            if not candidates:
                log(f"    [docx·资源] 警告：嵌入文件未下载到本地「{file_name}」token={old_tk}")
                results[block_id] = MediaResult(block_id, "file", file_name, success=False)
                continue

            actual_path = candidates[0]
            actual_name = os.path.basename(actual_path)

            # 重命名为原始文件名以便目标文档显示正确名称
            display_name = file_name if file_name != "file" else actual_name
            renamed_path = os.path.join(WORK_DIR, display_name)
            try:
                shutil.copy2(actual_path, renamed_path)
            except Exception:
                renamed_path = actual_path

            if not upload:
                # Block API 路径：只下载不上传，保留本地路径给 BFS 交错写入
                log(f"    [docx·资源] 嵌入文件「{file_name}」已下载（待交错写入）")
                results[block_id] = MediaResult(block_id, "file", file_name, success=True,
                                                local_path=renamed_path or actual_path)
                renamed_path = None  # 不要在 finally 中删除
                continue

            upload_file = f"./{os.path.basename(renamed_path or actual_path)}"

            # 上传到目标文档
            upload_cmd = ["lark-cli", "docs", "+media-insert",
                          "--doc", target_doc_id,
                          "--file", upload_file,
                          "--type", "file",
                          "--profile", tgt_profile, "--as", "user"]
            up_r = subprocess.run(upload_cmd, capture_output=True, text=True,
                                  timeout=180, cwd=WORK_DIR)
            if up_r.returncode != 0:
                log(f"    [docx·资源] 警告：嵌入文件上传到目标文档失败「{file_name}」token={old_tk}")
                results[block_id] = MediaResult(block_id, "file", file_name, success=False)
                continue

            try:
                up_json = json.loads(up_r.stdout[up_r.stdout.find('{'):])
                new_tk = up_json.get("data", {}).get("file_token")
            except Exception:
                new_tk = None

            if new_tk:
                log(f"    [docx·资源] 嵌入文件「{file_name}」已上传：{old_tk} → {new_tk}")
                results[block_id] = MediaResult(block_id, "file", file_name, success=True,
                                                target_token=new_tk)
            else:
                log(f"    [docx·资源] 警告：嵌入文件上传未返回 file_token「{file_name}」token={old_tk}")
                results[block_id] = MediaResult(block_id, "file", file_name, success=False)

        except Exception as e:
            log(f"    [docx·资源] 警告：嵌入文件处理异常「{file_name}」token={old_tk} — {e}")
            results[block_id] = MediaResult(block_id, "file", file_name, success=False)
        finally:
            import glob as _glob
            for f in _glob.glob(dl_abs + "*"):
                os.remove(f)
            if renamed_path and os.path.exists(renamed_path):
                os.remove(renamed_path)

    return results

# ============================================================
# 画板缩略图 (block_type=43)
# ============================================================

def process_boards(blocks, target_doc_id, src_profile, tgt_profile):
    """下载源文档中画板 (bt=43) 的缩略图，暂存本地。

    画板（白板）内容无法通过 API 跨组织迁移，
    但可以下载缩略图作为图片保留，让读者知道原来这里有什么。
    通过 docs +media-download --type whiteboard 下载 PNG 缩略图。

    注意：缩略图只下载不上传。上传由 write_blocks_bfs 在遇到画板 block 时
    即时执行（交错写入），确保缩略图出现在正确位置而非文档末尾。
    如果画板在嵌套容器内（非根级），BFS 会生成占位符文本。

    Returns:
        {block_id: MediaResult}  — success=True 时 local_path 指向缩略图文件
    """
    results = {}

    for block in blocks:
        if block.get("block_type") != 43:
            continue
        board = block.get("board", {})
        token = board.get("token", "")
        if not token:
            continue

        block_id = block["block_id"]
        dl_name = f"board_{token}"
        dl_abs = os.path.join(WORK_DIR, dl_name)

        try:
            import glob as _glob
            for f in _glob.glob(dl_abs + "*"):
                os.remove(f)

            # 下载画板缩略图
            dl_cmd = ["lark-cli", "docs", "+media-download",
                      "--token", token,
                      "--type", "whiteboard",
                      "--output", f"./{dl_name}",
                      "--profile", src_profile, "--as", "user"]
            dl_r = subprocess.run(dl_cmd, capture_output=True, text=True,
                                  timeout=120, cwd=WORK_DIR)
            if dl_r.returncode != 0:
                log(f"    [docx·资源] 警告：画板缩略图下载失败 token={token}")
                results[block_id] = MediaResult(block_id, "board", token, success=False)
                continue

            candidates = _glob.glob(dl_abs + "*")
            if not candidates:
                log(f"    [docx·资源] 警告：未下载到画板缩略图文件 token={token}")
                results[block_id] = MediaResult(block_id, "board", token, success=False)
                continue

            actual_path = candidates[0]
            log(f"    [docx·资源] 画板缩略图已下载：{token}")
            results[block_id] = MediaResult(block_id, "board", token, success=True,
                                            local_path=actual_path)

        except Exception as e:
            log(f"    [docx·资源] 警告：画板缩略图处理异常 token={token} — {e}")
            results[block_id] = MediaResult(block_id, "board", token, success=False)

    return results

# ============================================================
# 嵌入电子表格 (block_type=30)
# ============================================================

def process_embedded_sheets(blocks, state, src_profile, tgt_profile):
    """迁移文档中嵌入的电子表格视图 (bt=30)。

    飞书嵌入表格的 token 格式为 "{sheet_token}_{view_suffix}"，
    同一个 sheet 可能在文档中被多次引用（不同视图），只需导出导入一次。

    流程（对每个去重后的 sheet base_token）：
    1. drive +export 导出源表格为 xlsx 文件
    2. drive +import 导入到目标组织，得到新的 sheet token
    3. 记录 base_token 映射到 state.obj_mapping（后续数据复制需要）

    去重机制：sheet_map 缓存已处理的 base_token，避免重复导出导入。

    Returns:
        {block_id: MediaResult}，其中 src_token 和 target_token
        用于后续 post_process.py 的数据复制。
    """
    from strategies import _extract_import_token  # 延迟导入避免循环依赖

    results = {}
    sheet_map = {}  # 源 sheet base_token → 目标 base_token (或 None 表示失败)
    obj_mapping = state.get('obj_mapping', {})

    for block in blocks:
        if block.get("block_type") != 30:
            continue
        sheet_data = block.get("sheet") or block.get("view", {})
        token = sheet_data.get("token", "")
        if not token:
            continue

        block_id = block["block_id"]
        parts = token.rsplit('_', 1)
        base_token = parts[0] if len(parts) == 2 else token
        view_suffix = parts[1] if len(parts) == 2 else ""

        # 已处理过（同一 sheet 多次引用）
        if base_token in sheet_map:
            new_base = sheet_map[base_token]
            if new_base:
                new_token = f"{new_base}_{view_suffix}" if view_suffix else new_base
                results[block_id] = MediaResult(
                    block_id, "sheet", token, success=True,
                    target_token=new_token, src_token=token)
            else:
                results[block_id] = MediaResult(block_id, "sheet", token, success=False)
            continue

        # 检查是否在之前的迁移中已建立映射
        if base_token in obj_mapping:
            sheet_map[base_token] = obj_mapping[base_token]
            new_base = obj_mapping[base_token]
            new_token = f"{new_base}_{view_suffix}" if view_suffix else new_base
            log(f"    [docx·资源] 嵌入表格已存在映射，跳过导出：{base_token} → {new_base}")
            results[block_id] = MediaResult(
                block_id, "sheet", token, success=True,
                target_token=new_token, src_token=token)
            continue

        # 导出并导入电子表格
        node_dir = f"embed_sheet_{base_token}"
        node_dir_abs = os.path.join(WORK_DIR, node_dir)
        os.makedirs(node_dir_abs, exist_ok=True)

        try:
            log(f"    [docx·资源] 正在导出嵌入电子表格：{base_token}")
            run_lark(["drive", "+export",
                      "--token", base_token,
                      "--doc-type", "sheet",
                      "--file-extension", "xlsx",
                      "--output-dir", f"./{node_dir}",
                      "--overwrite"],
                     profile=src_profile, cwd=WORK_DIR, timeout=180)

            files = [f for f in os.listdir(node_dir_abs)
                     if os.path.isfile(os.path.join(node_dir_abs, f)) and not f.startswith('.')]
            if not files:
                log(f"    [docx·资源] 警告：表格导出后目录为空 token={base_token}")
                sheet_map[base_token] = None
                results[block_id] = MediaResult(block_id, "sheet", token, success=False)
                continue

            export_file = files[0]
            log(f"    [docx·资源] 正在导入电子表格到目标：{export_file}")
            imp = run_lark_json(
                ["drive", "+import",
                 "--file", f"./{node_dir}/{export_file}",
                 "--type", "sheet",
                 "--name", f"embedded_{base_token}"],
                profile=tgt_profile, cwd=WORK_DIR, timeout=180)

            new_base = _extract_import_token(imp, tgt_profile)
            if new_base:
                sheet_map[base_token] = new_base
                obj_mapping[base_token] = new_base
                new_token = f"{new_base}_{view_suffix}" if view_suffix else new_base
                log(f"    [docx·资源] 嵌入表格已迁移：{base_token} → {new_base}")
                results[block_id] = MediaResult(
                    block_id, "sheet", token, success=True,
                    target_token=new_token, src_token=token)
            else:
                log(f"    [docx·资源] 警告：表格导入未返回 token={base_token}")
                sheet_map[base_token] = None
                results[block_id] = MediaResult(block_id, "sheet", token, success=False)

        except Exception as e:
            log(f"    [docx·资源] 警告：嵌入表格迁移异常 token={base_token} — {e}")
            sheet_map[base_token] = None
            results[block_id] = MediaResult(block_id, "sheet", token, success=False)
        finally:
            shutil.rmtree(node_dir_abs, ignore_errors=True)

        time.sleep(0.5)

    if results:
        ok_count = sum(1 for r in results.values() if r.success)
        log(f"    [docx·资源] 嵌入表格块处理完毕：共 {len(results)} 处引用，成功 {ok_count} 处")

    return results
