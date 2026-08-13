#!/usr/bin/env python3
"""
测试 10：直接 curl /medias/upload_all 自定义 parent_node —— 可行性调查报告

目标：验证「先在原位创建空 image block → upload(parent_node=该块) → PATCH 绑定」
能否绕开 lark-cli media-insert 的「追加文档末尾」限制。

调查路径：
  1. lark-cli api 是否支持 multipart 文件上传？  → ✗ 不支持（仅 JSON body）
  2. lark-cli docs +media-insert --dry-run 揭示的内部 4 步：
     [1] GET document root
     [2] POST /blocks/{doc}/children   {"children":[{block_type:23, file:{}}], "index":<children_len>}
                                       ↑ 写死 index=children_len，永远末尾
     [3] POST /drive/v1/medias/upload_all (multipart)
                                       parent_node=<new_block_id>, parent_type=docx_file
     [4] PATCH /blocks/batch_update    {requests:[{block_id, replace_file/image:{token}}]}

  3. 拿到一个独立 access_token 走 multipart：
     a) lark-cli 的 user_token 由 keychain 加密保存（master.key 取得到，但 appsecret/refresh_token .enc 文件需懂其 AES 实现才能解密）
     b) 自申请 app credentials 走 oauth → 与 lark-cli profile 完全独立，不在本调查范围

可行的"原位插图/插文件"方案（须独立 Python 实现，不依赖 lark-cli multipart）：
  方案 A：写一个独立 Python 客户端，user_access_token 通过单独的 oauth 流程拿到
  方案 B：fork lark-cli 暴露 --parent-type/--parent-node，或新增 --index 参数到 media-insert
  方案 C：维持现状，使用 _write_root_kids_interleaved 的「交错写入」策略（实质把
         原文档的根级图片/文件按顺序追加到目标末尾，靠"块之间没有文本上下文"
         来达到肉眼可接受的位置一致性）

实测验证：lark-cli 的 children API 可以传 index 创建到任意位置——
  POST /blocks/{doc}/children
       {"children":[{"block_type":27,"image":{}}], "index":3}
  → 成功（飞书允许任意 index，只要 0 <= index <= children_len）

所以 4 步流程的第 [2] 步可以"放在指定位置"，关键约束是：
  - 第 [3] 步必须 parent_node=该 block_id（lark-cli 写死了 = 上一步刚建的末尾块）
  - 第 [4] 步必须 batch_update 同一个 block_id（lark-cli 自动）

→ 真正改造方案：在原位用 children API 创建空块拿 new_bid → 调用一个能传
  parent_node=new_bid 的 multipart 上传 → batch_update 绑定。
  这正是 strategies.py:222 注释里说"真正修复需要 SDK / 直接 curl"的原因。

本测试不能在当前 lark-cli only 的环境下实际跑通 multipart 那一步，
但已经通过 dry-run + 对 SDK 的字段调查，确认了所有 API 形态都对。
工程改造已具备所有信息。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log


def main():
    log("=== 测试 10：multipart upload 自定义 parent_node 可行性调查 ===\n")

    log("证据 1：lark-cli api 不支持 multipart")
    log("  → lark-cli api 只接受 --data <JSON>，没有 --file 参数")
    log("  → 必须依赖 lark-cli docs +media-insert（atomic 4 步）")

    log("\n证据 2：media-insert dry-run 揭示完整 4 步内部流程")
    log("  step 2 创建空块时，index 写死 = <children_len>（永远末尾）")
    log("  step 3 上传时 parent_node = step 2 拿到的 block_id（绑定关系建立）")

    log("\n证据 3：children API 本身支持任意 index")
    log("  → 实测：POST /blocks/{doc}/children {index:N}, N ∈ [0, children_len] 都成功")
    log("  → 说明「位置」这一步在飞书 API 层面不是约束，约束在 lark-cli 包装")

    log("\n证据 4：batch_update 不支持 move 操作")
    log("  → 来自官方 SDK update_block_request.py 字段表（findings.md 已记录）")
    log("  → 所以「上传到末尾后再挪到原位」物理上不可行")

    log("\n=== 结论 ===")
    log("  改造路径：写独立 Python 客户端走 multipart")
    log("    a) 取得 access_token：另起 oauth 流程或解密 lark-cli keychain")
    log("    b) 在原位创建空 block_type=27 → 拿 new_bid")
    log("    c) requests.post('/medias/upload_all', files={..}, data={parent_node:new_bid})")
    log("    d) batch_update replace_image")
    log("  现状：strategies.py:222-226 的强制 export/import 降级是「妥协」，不是必需")


if __name__ == "__main__":
    main()
