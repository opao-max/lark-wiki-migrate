#!/usr/bin/env python3
"""
测试 9：自造最小 .xmind 导入到目标，验证 mindnote 是否真的不能创建。

XMind 8 文件 = ZIP，内含 content.xml + manifest.xml + meta.xml。
最小可识别结构：
  content.xml — 主题树
  META-INF/manifest.xml — 文件清单
  meta.xml — 元数据

用法：
  python3 tools/test9_mindnote_import.py --profile xupt
"""
import argparse, sys, os, json, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from common import lark_api, log, WORK_DIR, run_lark_json


def make_minimal_xmind(path):
    content_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<xmap-content xmlns="urn:xmind:xmap:xmlns:content:2.0" xmlns:fo="http://www.w3.org/1999/XSL/Format" xmlns:svg="http://www.w3.org/2000/svg" xmlns:xhtml="http://www.w3.org/1999/xhtml" xmlns:xlink="http://www.w3.org/1999/xlink" version="2.0">
<sheet id="sheet1"><topic id="root"><title>Hello XMind</title><children><topics type="attached">
<topic id="t1"><title>Subtopic 1</title></topic>
<topic id="t2"><title>Subtopic 2</title></topic>
</topics></children></topic><title>Sheet1</title></sheet>
</xmap-content>'''
    manifest_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<manifest xmlns="urn:xmind:xmap:xmlns:manifest:1.0" password-hint="">
<file-entry full-path="content.xml" media-type="text/xml"/>
<file-entry full-path="META-INF/" media-type=""/>
<file-entry full-path="META-INF/manifest.xml" media-type="text/xml"/>
<file-entry full-path="meta.xml" media-type="text/xml"/>
</manifest>'''
    meta_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="no"?>
<meta xmlns="urn:xmind:xmap:xmlns:meta:2.0" version="2.0">
<Creator><Name>test9</Name><Version>1.0</Version></Creator>
</meta>'''
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('content.xml', content_xml)
        z.writestr('META-INF/manifest.xml', manifest_xml)
        z.writestr('meta.xml', meta_xml)


def try_import(profile, file_token, file_ext, type_, label):
    log(f"\n  → 尝试 {label}: ext={file_ext}, type={type_}")
    body = {
        "file_extension": file_ext,
        "file_token": file_token,
        "type": type_,
        "file_name": f"test9_{label}_{int(time.time())}",
        "point": {"mount_type": 1, "mount_key": ""},
    }
    try:
        r = lark_api("POST", "/open-apis/drive/v1/import_tasks",
                     data=body, profile=profile)
        log(f"    创建 task code={r.get('code')}, msg={r.get('msg','')[:120]}")
        ticket = r.get("data", {}).get("ticket")
        if not ticket:
            return False
        for _ in range(15):
            time.sleep(2)
            tr = lark_api("GET", f"/open-apis/drive/v1/import_tasks/{ticket}",
                          profile=profile)
            res = tr.get("data", {}).get("result", {})
            st = res.get("job_status", -1)
            if st == 0:
                log(f"    ✓ 完成！token={res.get('token')}")
                return True
            if st not in (1, 2):
                log(f"    ✗ 失败 status={st}, msg={res.get('job_error_msg','')[:200]}")
                return False
        log("    ✗ 超时")
    except Exception as e:
        log(f"    ✗ 异常：{str(e)[:300]}")
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True)
    args = p.parse_args()
    os.makedirs(WORK_DIR, exist_ok=True)

    log("=== 测试 9：自造 .xmind import → mindnote ===\n")

    xmind_path = os.path.join(WORK_DIR, "test9_min.xmind")
    make_minimal_xmind(xmind_path)
    log(f"步骤 1：本地造 .xmind ({os.path.getsize(xmind_path)} bytes)")

    log("\n步骤 2：上传到 drive")
    up = run_lark_json(
        ["drive", "+upload", "--file", "./test9_min.xmind"],
        profile=args.profile, cwd=WORK_DIR, timeout=60)
    file_token = up.get("data", {}).get("file_token")
    if not file_token:
        log(f"  上传失败：{json.dumps(up, ensure_ascii=False)[:200]}")
        return
    log(f"  得到 file_token={file_token}")

    log("\n步骤 3：尝试不同 ext/type 组合 import 为 mindnote")
    cases = [
        ("xmind",    "mindnote", "xmind→mindnote"),
        ("xmind",    "mindmap",  "xmind→mindmap"),
        ("xmind",    "mind",     "xmind→mind"),
    ]
    results = []
    for ext, t, label in cases:
        ok = try_import(args.profile, file_token, ext, t, label)
        results.append((label, ok))

    log("\n=== 汇总 ===")
    for label, ok in results:
        log(f"  {label} → {'✓' if ok else '✗'}")


if __name__ == "__main__":
    main()
