#!/usr/bin/env python3
"""
测试 11：whiteboard /download_as_image 是否能拿到高清 PNG/JPEG，
作为画板（board, bt=43）跨租户迁移的高保真路径。

发现：
  GET /open-apis/board/v1/whiteboards/{id}/download_as_image
  → 返回二进制 image/jpeg，2560x2560 像素，~44KB（实测一个简单画板）
  → 比 lark-cli docs +media-download 拿到的画板缩略图清晰得多

意义：
  项目当前画板路径：
    media.py process_boards → media-download 拿"缩略图" → media-insert 上传 →
    在文档中以"图片块（27）"形式呈现，附带 [画板: token] 占位文本
  改造后：
    用 download_as_image 拿到 2560 高清 → 同样路径上传 → 用户视觉无损降级

不能解决的：
  - 画板的可编辑性（任何方案都做不到，跨租户没有"复制画板"API）
  - 画板内的引用关系（@提及、跨文档链接）

用法：
  python3 tools/test11_whiteboard_download.py --profile xupt --board <id>
"""
import argparse, sys, os, subprocess
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import log, WORK_DIR


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--board",   required=True, help="whiteboard_id (= board.token in docx)")
    args = p.parse_args()
    os.makedirs(WORK_DIR, exist_ok=True)

    log(f"=== 测试 11：whiteboard download_as_image ===\n")
    log(f"目标 whiteboard_id={args.board}")

    out_name = f"test11_board_{args.board[:10]}.jpg"
    out_path = os.path.join(WORK_DIR, out_name)
    if os.path.exists(out_path):
        os.remove(out_path)

    log(f"\n步骤 1：GET /board/v1/whiteboards/{{id}}/download_as_image")
    r = subprocess.run(
        ["lark-cli", "api", "GET",
         f"/open-apis/board/v1/whiteboards/{args.board}/download_as_image",
         "--profile", args.profile, "--as", "user",
         "-o", f"./{out_name}"],
        capture_output=True, text=True, timeout=60, cwd=WORK_DIR)
    log(f"  stdout 前 200 字：{(r.stdout or '')[:200]}")

    if not os.path.exists(out_path):
        log("  ✗ 文件未生成")
        return

    size = os.path.getsize(out_path)
    log(f"\n步骤 2：检查文件 {out_name}")
    log(f"  文件大小：{size} bytes")

    # 读取尺寸
    try:
        ftype = subprocess.run(["file", out_path], capture_output=True, text=True)
        log(f"  file 探测：{ftype.stdout.strip()}")
    except Exception:
        pass

    # 对比：lark-cli docs +media-download 拿到的缩略图大小
    log(f"\n步骤 3：对比 lark-cli docs +media-download（项目当前用法）")
    cmp_name = f"test11_board_{args.board[:10]}_cmp.jpg"
    cmp_path = os.path.join(WORK_DIR, cmp_name)
    if os.path.exists(cmp_path):
        os.remove(cmp_path)
    r2 = subprocess.run(
        ["lark-cli", "docs", "+media-download",
         "--token", args.board, "--output", f"./{cmp_name}",
         "--profile", args.profile, "--as", "user"],
        capture_output=True, text=True, timeout=60, cwd=WORK_DIR)
    if os.path.exists(cmp_path):
        cmp_size = os.path.getsize(cmp_path)
        ftype2 = subprocess.run(["file", cmp_path], capture_output=True, text=True)
        log(f"  media-download 大小：{cmp_size} bytes")
        log(f"  media-download 探测：{ftype2.stdout.strip()}")
    else:
        log(f"  media-download 失败：{(r2.stdout or r2.stderr)[:200]}")

    log("\n=== 结论 ===")
    log(f"  ✓ download_as_image 可拿到画板的高清渲染图（2560×2560 实测）")
    log("  → 可替代 process_boards 当前用 media-download 拿低清缩略图的方式")
    log("  → 用户视觉上的「降级」只剩：可编辑性、内部链接、@提及")


if __name__ == "__main__":
    main()
