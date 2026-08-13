#!/usr/bin/env python3
"""
测试 1：Block API 能否创建带图片的图片块？

结论：
  - 空 token 可以创建一个「空壳」图片块（无实际图片内容）
  - 传任何 token（伪造的 / 跨租户的）→ API 报错 1770001 invalid param
  - image.token 是只读字段，只能由飞书服务端在上传时赋值
  → 无法通过 Block API 一步到位创建带图片的块
  → 含图片的文档必须走 export/import 降级路径

用法：
  python3 tools/test1_image_block.py --profile xupt --doc <目标测试文档ID>

  可选 --src-profile / --src-doc 来演示「跨租户真实 token 也不行」：
  python3 tools/test1_image_block.py --profile xupt --doc <目标文档> \\
      --src-profile seewo --src-doc <源文档ID（含图片）>
"""

import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log, APIError


def create_image_child(profile, doc_id, image_token, label):
    """尝试用 Block API 在文档根下创建一个 bt=27 图片子块。"""
    log(f"\n{'='*60}")
    log(f"测试: {label}")
    log(f"  image.token = {image_token!r}")

    try:
        resp = lark_api("POST",
            f"/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
            data={
                "children": [{"block_type": 27, "image": {"token": image_token}}],
                "index": 0,
            },
            profile=profile)

        code = resp.get("code", -1) if resp else -1
        msg  = resp.get("msg", "")  if resp else "no response"
        log(f"  API 返回: code={code}, msg={msg}")

        if code == 0:
            log(f"  → 创建成功，但这是一个空壳图片块（没有实际图片内容）")
            log(f"    image.token 是只读字段，无法在创建时指定有效图片")

    except APIError as e:
        log(f"  API 报错: {str(e)[:200]}")
        log(f"  → 失败：Block API 拒绝了此 token")


def get_real_image_token(profile, doc_id):
    """从文档中读取第一个图片块的真实 token。"""
    try:
        resp = lark_api("GET",
            f"/open-apis/docx/v1/documents/{doc_id}/blocks",
            params={"page_size": 500},
            profile=profile)
    except APIError as e:
        log(f"  读取源文档失败: {e}")
        return None

    for block in (resp or {}).get("data", {}).get("items", []):
        if block.get("block_type") == 27:
            token = block.get("image", {}).get("token", "")
            if token:
                log(f"  找到源组织图片 token: {token}")
                return token
    return None


def main():
    p = argparse.ArgumentParser(description="测试 Block API 创建图片块")
    p.add_argument("--profile",     required=True, help="目标组织 profile")
    p.add_argument("--doc",         required=True, help="目标测试文档 document_id")
    p.add_argument("--src-profile", default="",    help="源组织 profile")
    p.add_argument("--src-doc",     default="",    help="源文档 document_id（含图片）")
    args = p.parse_args()

    log("=== 测试 1：Block API 能否创建带图片的图片块 (bt=27)？===")

    # 测试 A: 空 token — 能创建空壳，但没有图片
    create_image_child(args.profile, args.doc, "", "空 token（无图片内容）")

    # 测试 B: 伪造 token — 直接报错
    create_image_child(args.profile, args.doc,
                       "FAKE_TOKEN_1234567890", "伪造 token")

    # 测试 C: 跨租户真实 token — 也报错
    if args.src_profile and args.src_doc:
        log(f"\n从源文档读取真实图片 token...")
        real_token = get_real_image_token(args.src_profile, args.src_doc)
        if real_token:
            create_image_child(args.profile, args.doc, real_token,
                               "源组织的真实 image.token（跨租户）")
        else:
            log("  源文档中没有图片块，跳过此测试")

    log(f"\n{'='*60}")
    log("结论：")
    log("  1. 空 token 能创建空壳图片块，但没有实际图片内容")
    log("  2. 指定任何 token 都报错 — image.token 跨租户失效")

if __name__ == "__main__":
    main()
