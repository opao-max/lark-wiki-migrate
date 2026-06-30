"""后处理脚本共用的 docx blocks 读取（宽松错误处理）。

与 `block_api.read_docx_blocks` 不同：这里遇到 API 非零 code 时静默停止分页，
而不是抛异常，供 fix-links / fix-mentions 等脚本使用。
"""

import time

from common import lark_api


def read_doc_blocks(doc_id, profile, *, page_sleep=0.3):
    """分页读取 docx 文档的全部 blocks。"""
    all_blocks = []
    page_token = None
    while True:
        params = {"page_size": "500", "document_revision_id": "-1"}
        if page_token:
            params["page_token"] = page_token
        r = lark_api(
            "GET",
            f"/open-apis/docx/v1/documents/{doc_id}/blocks",
            params=params,
            profile=profile,
        )
        if not r or r.get("code", -1) != 0:
            break
        all_blocks.extend(r.get("data", {}).get("items", []))
        page_token = r.get("data", {}).get("page_token")
        if not page_token:
            break
        time.sleep(page_sleep)
    return all_blocks
