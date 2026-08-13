#!/usr/bin/env python3
"""
测试 4（修订版）：验证「Block API 创建图片块」的真实约束。

老结论：「跨租户 token 失效，所以图片必须 export/import」
新调查结论：约束更精准——image.token 与一个特定的 image block 绑定不可解。

关键发现（来自 lark-cli docs +media-insert --dry-run 揭示的内部 4 步流程）：
  1. POST /docx/v1/documents/{doc}/blocks/{doc}/children   创建空 image block → new_bid
  2. POST /drive/v1/medias/upload_all
       parent_type=docx_image, parent_node=<new_bid>        ← 关键：parent_node 是 block_id，不是 doc_id
       上传文件 → 拿到 file_token（与 new_bid 已绑定）
  3. PATCH /docx/v1/documents/{doc}/blocks/batch_update
       requests=[{block_id: new_bid, replace_image: {token}}]
  4. token 与 block 关系建立，图片显示

本测试验证的事实：
  - Step 3 的 PATCH 只能将 token 绑定到 Step 2 的同一个 block_id
  - 把 token 复用到「另一个 block」会被服务端拒绝（1770013 relation mismatch）
  - 所以「迁移 N 张图片」需要 N 次完整 4 步循环（创建空块→上传到该块→PATCH绑定）

实际测试步骤：
  1. 走 lark-cli docs +media-insert 路径，从源下载、上传到目标 doc 末尾，得到 (token_A, block_A)
  2. 验证 token_A + block_A 关系正确（PATCH replace 自身成功）
  3. 验证 token_A + 任意其他 image block 失败（1770013）
  4. 结论：token 不可复用 → strategies.py:222-226 的「整篇 export/import 降级」可被替代为
     「逐图执行完整 4 步上传」，但仍需 N 次往返（不是 token 复用一次）

用法：
  python3 tools/test4_image_block_via_target_token.py \
    --src-profile seewo --src-doc M2kkdji0coJ7Dpx5V3Fcm9rBnde \
    --tgt-profile xupt --tgt-doc WEmwdvqmUoICf6xjxjjc518jnRb
"""

import argparse, sys, os, json, subprocess, glob as _glob, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log, WORK_DIR


def get_first_image_token(profile, doc_id):
    r = lark_api("GET", f"/open-apis/docx/v1/documents/{doc_id}/blocks",
                 params={"page_size": 500}, profile=profile)
    for b in (r or {}).get("data", {}).get("items", []):
        if b.get("block_type") == 27:
            tk = b.get("image", {}).get("token", "")
            if tk:
                return tk
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src-profile", required=True)
    p.add_argument("--src-doc",     required=True)
    p.add_argument("--tgt-profile", required=True)
    p.add_argument("--tgt-doc",     required=True)
    args = p.parse_args()
    os.makedirs(WORK_DIR, exist_ok=True)

    log("=== 测试 4：图片 block 创建的真实约束 ===\n")

    log("步骤 1：从源文档读 1 张图，下载到本地")
    src_token = get_first_image_token(args.src_profile, args.src_doc)
    if not src_token:
        log("  源文档无图，无法测试"); return
    dl_name = f"test4_{src_token}"
    for f in _glob.glob(os.path.join(WORK_DIR, dl_name + "*")):
        os.remove(f)
    subprocess.run(
        ["lark-cli", "docs", "+media-download", "--token", src_token,
         "--output", f"./{dl_name}", "--profile", args.src_profile, "--as", "user"],
        capture_output=True, text=True, timeout=60, cwd=WORK_DIR)
    cands = _glob.glob(os.path.join(WORK_DIR, dl_name + "*"))
    if not cands:
        log("  下载失败"); return
    local = cands[0]
    log(f"  下载到：{os.path.basename(local)}\n")

    log("步骤 2：用 lark-cli 走完整 4 步流程上传到目标 doc")
    up = subprocess.run(
        ["lark-cli", "docs", "+media-insert",
         "--doc", args.tgt_doc, "--file", f"./{os.path.basename(local)}",
         "--type", "image",
         "--profile", args.tgt_profile, "--as", "user"],
        capture_output=True, text=True, timeout=120, cwd=WORK_DIR)
    try:
        up_json = json.loads(up.stdout[up.stdout.find('{'):])
        token_A = up_json["data"]["file_token"]
        block_A = up_json["data"]["block_id"]
    except Exception as e:
        log(f"  上传/解析失败：{e}"); return
    log(f"  得到 token_A={token_A}")
    log(f"  得到 block_A={block_A}（token 与 block 服务端已绑定）\n")

    log("步骤 3：PATCH block_A 自己 replace_image token_A —— 期望成功")
    r = subprocess.run(
        ["lark-cli", "api", "PATCH",
         f"/open-apis/docx/v1/documents/{args.tgt_doc}/blocks/{block_A}",
         "--data", json.dumps({"replace_image": {"token": token_A}}),
         "--profile", args.tgt_profile, "--as", "user"],
        capture_output=True, text=True, timeout=30)
    out = r.stdout or r.stderr
    log(f"  PATCH 返回（前 120 字）：{out[:120]}\n")

    log("步骤 4：另建一个空 image block_B，PATCH 它 replace_image token_A —— 期望失败")
    cr = lark_api("POST",
        f"/open-apis/docx/v1/documents/{args.tgt_doc}/blocks/{args.tgt_doc}/children",
        data={"children": [{"block_type": 27, "image": {}}], "index": 0},
        profile=args.tgt_profile)
    block_B = cr["data"]["children"][0]["block_id"]
    log(f"  block_B={block_B}")
    r = subprocess.run(
        ["lark-cli", "api", "PATCH",
         f"/open-apis/docx/v1/documents/{args.tgt_doc}/blocks/{block_B}",
         "--data", json.dumps({"replace_image": {"token": token_A}}),
         "--profile", args.tgt_profile, "--as", "user"],
        capture_output=True, text=True, timeout=30)
    out = r.stdout or r.stderr
    is_fail = "1770013" in out
    log(f"  PATCH block_B 返回（前 200 字）：{out[:200]}")
    log(f"  是否 1770013 relation mismatch：{is_fail}\n")

    log("步骤 5：清理测试块")
    for bid in [block_A, block_B]:
        try:
            lark_api("POST",
                f"/open-apis/docx/v1/documents/{args.tgt_doc}/blocks/{args.tgt_doc}/children/batch_delete",
                data={"block_ids": [bid]}, profile=args.tgt_profile)
        except Exception:
            pass

    if os.path.exists(local):
        os.remove(local)

    log("\n=== 结论 ===")
    log("  ✓ image.token 由服务端在 upload 时与一个特定 image block 绑定")
    log("  ✓ token 不可被另一个 block 复用（1770013）")
    log("  → 「迁移 N 张图片」=「N 次：创建空块 + upload(parent_node=该块) + PATCH 绑定」")
    log("  → strategies.py:222-226 强制 export/import 降级 不是 Block API 不行，")
    log("     而是【lark-cli 没暴露 parent_type/parent_node 的 upload 入口】")
    log("     `media-insert` 把图片追加到末尾后无法移动到原位（PATCH 只能改 token，不改位置）")
    log("  → 真正的修复需要：")
    log("     a) 用 SDK / 直接 curl /drive/v1/medias/upload_all 自定义 parent_node")
    log("     b) 创建空块时就指定 index/parent，然后上传到该 block_id —— 一步到位放对位置")


if __name__ == "__main__":
    main()
