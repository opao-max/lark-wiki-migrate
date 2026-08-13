#!/usr/bin/env python3
"""
测试 8：import_task 的 `point` 字段能否「直接挂到 wiki 节点」，省去 move_docs_to_wiki。

SDK 字段（来自 drive/v1/model/import_task_mount_point.py）：
  point: { mount_type: int, mount_key: str }

OpenAPI 现行约定：
  mount_type=1 → mount 到「我的空间」根目录，mount_key=""
  mount_type=2 → mount 到 wiki，mount_key=wiki_node_token

如果 point=2 真能直挂 wiki，strategies.py 里所有 migrate_* 函数就能省一次
move_docs_to_wiki 调用 + 异步轮询。

流程：
  1. 本地造一个最小 .docx 文件（仅含 "Hello from import_task point test"）
  2. /open-apis/drive/v1/medias/upload_all 上传到 ccm_import_open，拿 file_token
  3. POST /open-apis/drive/v1/import_tasks，body 带 point.mount_type=2/mount_key=<wiki_node>
  4. 轮询 import_tasks/{ticket} 直到完成
  5. 检查返回的 token 是否已经在 wiki 节点下（GET wiki node 的 children）

用法：
  python3 tools/test8_import_task_point.py --profile xupt \
    --space 7631132924302789820 \
    --parent SVVawXAEJiZErYkuzPkcOn7cn5c
"""
import argparse, sys, os, json, time, subprocess, zipfile, io
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log, WORK_DIR, run_lark_json


def make_minimal_docx(path):
    """最小合法 .docx（一个段落 "test point"）"""
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>'''
    rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''
    doc_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body><w:p><w:r><w:t>Hello from import_task point test</w:t></w:r></w:p></w:body>
</w:document>'''
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types)
        z.writestr('_rels/.rels', rels)
        z.writestr('word/document.xml', doc_xml)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    p.add_argument("--space",   required=True, help="wiki space_id")
    p.add_argument("--parent",  required=True, help="wiki parent node_token")
    args = p.parse_args()
    os.makedirs(WORK_DIR, exist_ok=True)

    log("=== 测试 8：import_task point 直挂 wiki ===\n")

    # ---- 1. 造最小 docx ----
    docx_path = os.path.join(WORK_DIR, "test8_point.docx")
    make_minimal_docx(docx_path)
    size = os.path.getsize(docx_path)
    log(f"步骤 1：本地造 {docx_path} ({size} bytes)")

    # ---- 2. 上传到 drive ccm_import_open ----
    # lark-cli api 不支持 multipart，改用 lark-cli drive +upload
    log("\n步骤 2：lark-cli drive +upload 拿 file_token")
    up = run_lark_json(
        ["drive", "+upload", "--file", "./test8_point.docx"],
        profile=args.profile, cwd=WORK_DIR, timeout=60)
    file_token = (up or {}).get("data", {}).get("file_token")
    if not file_token:
        log(f"  上传失败：{json.dumps(up, ensure_ascii=False)[:300]}")
        return
    log(f"  得到 file_token={file_token}")

    # ---- 3. 创建 import_task，带 point ----
    log(f"\n步骤 3：POST /import_tasks，point.mount_type=2, mount_key={args.parent}")
    body = {
        "file_extension": "docx",
        "file_token": file_token,
        "type": "docx",
        "file_name": f"test8_point_{int(time.time())}",
        "point": {
            "mount_type": 2,
            "mount_key": args.parent,
        },
    }
    log(f"  body={json.dumps(body, ensure_ascii=False)}")
    r = lark_api("POST", "/open-apis/drive/v1/import_tasks",
                 data=body, profile=args.profile)
    log(f"  返回 code={r.get('code')}, msg={r.get('msg','')[:120]}")
    ticket = (r or {}).get("data", {}).get("ticket")
    if not ticket:
        log(f"  没拿到 ticket：{json.dumps(r, ensure_ascii=False)[:300]}")
        return
    log(f"  ticket={ticket}")

    # ---- 4. 轮询 ----
    log("\n步骤 4：轮询任务直到完成")
    final = None
    for i in range(30):
        time.sleep(2)
        tr = lark_api("GET", f"/open-apis/drive/v1/import_tasks/{ticket}",
                      profile=args.profile)
        res = (tr or {}).get("data", {}).get("result", {})
        status = res.get("job_status", -1)
        log(f"  [{i+1}] status={status}, msg={res.get('job_error_msg','')[:80]}")
        if status == 0:
            final = res
            tk = res.get("token", "")
            log(f"\n  ✓ 完成！token={tk}")
            break
        if status not in (1, 2):  # 1=initiated, 2=processing
            final = res
            log(f"\n  ✗ 任务失败：{res}")
            break

    if not final or not final.get("token"):
        return

    new_token = final["token"]

    # ---- 5. 验证：列 parent 下的 children，看新 token 在不在 ----
    log("\n步骤 5：列 wiki parent 下的 children，验证是否已挂载")
    ch = lark_api("GET",
        f"/open-apis/wiki/v2/spaces/{args.space}/nodes",
        params={"parent_node_token": args.parent, "page_size": 50},
        profile=args.profile)
    items = (ch or {}).get("data", {}).get("items", [])
    log(f"  parent 下共 {len(items)} 个子节点")
    found = False
    for n in items:
        if n.get("obj_token") == new_token:
            found = True
            log(f"  ✓ 找到了！node_token={n.get('node_token')}, title={n.get('title')}")
            break
    if not found:
        log(f"  ✗ 未在 parent 下找到 token={new_token}")
        log(f"  → import 后文档去哪儿了？检查「我的空间」")

    log("\n=== 结论 ===")
    if found:
        log("  ✓ import_task.point.mount_type=2 可以直挂 wiki，省去 move_docs_to_wiki")
    else:
        log("  ✗ point 未生效，仍需 move_docs_to_wiki")


if __name__ == "__main__":
    main()
