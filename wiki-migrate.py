#!/usr/bin/env python3
"""
飞书跨组织知识库迁移工具

将飞书源组织的知识库完整迁移到另一个组织。
支持 docx(Block API)/doc/sheet/bitable(export→import)/file(download→upload)/shortcut。
mindnote/slides 创建占位文档。

用法:
  # 扫描（--target-root 指定目标知识库中的挂载父节点）
  python wiki-migrate.py scan   --source-profile seewo --target-profile seewo-gz --source-space <id> --target-space <id> --target-root <node_token>
  python wiki-migrate.py scan   --from-file nodes.json --source-profile seewo --target-profile seewo-gz --source-space <id> --target-space <id> --target-root <node_token>

  # 执行/断点续传
  python wiki-migrate.py run    --state migration-state.json
  python wiki-migrate.py resume --state migration-state.json

  # 验证
  python wiki-migrate.py verify --state migration-state.json [--all]

  # 预检
  python wiki-migrate.py precheck --state migration-state.json

  # 单节点：无需 scan，凭 node_token 查询后写入 state 并迁移
  python wiki-migrate.py migrate-one --source-profile seewo --target-profile xupt \\
    --target-space <目标空间ID> --node-token <源节点node_token> [--state single.json] [--target-parent <目标父节点token>]

后处理（迁移完成后依次执行）:
  python fix-links.py all --state migration-state.json [--dry-run]
  python fix-mentions.py all --state migration-state.json [--fix]
  python migrate-comments.py all --state migration-state.json [--dry-run]
  python mindnote-export.py login --profile <profile>
  python mindnote-export.py transfer --state migration-state.json --all

安全: 源组织仅读取，目标组织仅写入。

实现说明:
  子命令逻辑在 `migrate/wiki_commands.py`，本文件仅保留入口与 Ctrl+C 提示。
"""

import signal
import sys

from common import log
from migrate.wiki_commands import main as wiki_main


def _sigint_handler(sig, frame):
    log("\nInterrupted (Ctrl+C). State already saved after last completed node.")
    sys.exit(2)


signal.signal(signal.SIGINT, _sigint_handler)


if __name__ == "__main__":
    sys.exit(wiki_main())
