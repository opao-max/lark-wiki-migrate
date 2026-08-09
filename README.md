# 飞书跨组织知识库迁移工具

将一个飞书组织的知识库完整迁移到另一个组织，支持 docx / doc / sheet / bitable / file / mindnote / shortcut / slides。

迁移过程对源组织只读，所有写入操作均在目标组织。

## 前置条件

- **Python 3.8+**
- **lark-cli**（飞书命令行工具），需配置源组织和目标组织各一个 profile
- **playwright** + **Chrome 浏览器**（仅 mindnote 迁移需要）

```bash
# 安装 playwright（可选，仅迁移思维导图时需要）
pip install playwright
```

## 文件说明

### 代码结构（推荐阅读顺序）

```
lark_wiki_migirate/
├── common.py              ← 1. 先读这个：公共基础设施
├── media.py               ← 2. 媒体资源迁移（图片/文件/表格/画板下载上传）
├── block_api.py           ← 3. Block 操作（读/清洗/写 blocks）
├── strategies.py          ← 4. 各类型迁移策略（docx/doc/sheet/file/...）
├── post_process.py        ← 5. 迁移后处理（占位符统一/表格数据复制）
├── wiki-migrate.py        ← 6. CLI 入口（薄封装：信号 + 调用子命令实现）
├── migrate/               ← 7. 内部实现包
│   ├── wiki_commands.py   ← scan / run / verify / precheck / migrate-one 的实现
│   └── doc_blocks.py      ← 后处理脚本共用的 docx blocks 读取（宽松错误处理）
├── fix-links.py           ← 8. 后处理：文档间互链修复
├── fix-mentions.py        ← 9. 后处理：@提及修复
├── migrate-comments.py    ← 10. 后处理：评论迁移
├── mindnote-export.py     ← 11. 后处理：思维导图浏览器自动化
├── scan-embeds.py         ← 12. 辅助工具：扫描嵌入内容类型分布
└── README.md
```

**模块依赖关系：**

```
common.py          ← 所有文件都依赖它（日志、lark-cli 封装、状态管理、异常类）
  ↑
media.py           ← 依赖 common；被 strategies.py 使用
  ↑                   返回 MediaResult（显式数据结构，不修改 blocks）
block_api.py       ← 依赖 common；被 strategies.py 使用
  ↑                   接收 MediaResult 决定生成什么占位符
post_process.py    ← 依赖 common + block_api；被 strategies.py 使用
  ↑
strategies.py      ← 组装 media + block_api + post_process，实现各迁移策略
  ↑
wiki-migrate.py    ← CLI 入口（薄封装）
migrate/wiki_commands.py ← 组装 block_api + strategies，实现各子命令

fix-links.py       ← 依赖 common + migrate.doc_blocks
fix-mentions.py    ← 依赖 common + migrate.doc_blocks
migrate-comments.py ← 仅依赖 common
mindnote-export.py  ← 仅依赖 common
scan-embeds.py      ← 独立脚本，自带 lark-cli 调用逻辑
```

### 各文件职责


| 文件                         | 行数   | 职责                                                                                                                                                                          |
| -------------------------- | ---- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `common.py`                | ~290 | 异常类层级、`log()`、`run_lark()` 自动重试封装、`lark_api()` REST 调用、状态文件原子读写                                                                                                             |
| `media.py`                 | ~500 | `MediaResult` 数据类、`process_images/files/sheets/boards()` 媒体下载上传，返回 `{block_id: MediaResult}`                                                                               |
| `block_api.py`             | ~760 | `read_docx_blocks()` 分页读取、`clean_block()` 30+ 种 block 类型清洗（接收 MediaResult）、`write_blocks_bfs()` BFS 逐层写入（含 comment_ids 清除、write_index 自校准）                                 |
| `post_process.py`          | ~460 | 嵌入表格/文件/画板资源复位（交错写入 + Block API 唯一化定位）、占位符统一                                                                                                                                |
| `strategies.py`            | ~820 | `migrate_docx()` Block API 高保真迁移（含 export/import 回退）、`migrate_export_import()` doc/sheet/bitable、`migrate_file()` 下载上传（保留原始文件名）、`migrate_placeholder()` 占位、`migrate_shortcut()` 快捷方式 |
| `wiki-migrate.py`          | ~60  | CLI 入口：注册 Ctrl+C 提示，转发到 `migrate/wiki_commands.py`                                                                                                                          |
| `migrate/wiki_commands.py` | ~1090 | `cmd_scan()` 全量或 `--from-file` 建 state；`cmd_migrate_one()` 单节点；`cmd_run()` 全量；`cmd_verify()` / `cmd_precheck()` / `cmd_report()` 迁移报告                                      |
| `migrate/doc_blocks.py`    | ~30  | 后处理共用的 docx blocks 分页读取（API 失败时静默停止）                                                                                                                                        |
| `fix-links.py`             | ~360 | 扫描目标文档 blocks 中的飞书 URL，替换源 token 为目标 token + 域名替换                                                                                                                           |
| `fix-mentions.py`          | ~480 | 修复 `mention_user`（替换 open_id）和纯文本 `@某某`（恢复为原生提及）；通过邮箱/手机/姓名自动映射                                                                                                            |
| `migrate-comments.py`      | ~450 | 读取源评论 → 解析用户名 → 匹配锚定位置 → 在目标创建评论                                                                                                                                            |
| `mindnote-export.py`       | ~680 | Playwright 浏览器自动化：登录 → 导出 .mm → 解析 XML → 键盘输入到目标                                                                                                                            |
| `scan-embeds.py`           | ~390 | 并行扫描知识库所有 docx/doc 的 block 类型分布和嵌入内容统计                                                                                                                                       |


### 核心设计：MediaResult 消除隐式通信

旧设计中，`process_embedded_files()` 在 block 上挂 `_file_inserted=True`，然后 `clean_block()` 读这个 flag 决定生成什么占位符。读者必须在两个文件之间来回跳才能理解数据流。

新设计中，`process_*` 函数返回 `{block_id: MediaResult}` 字典，`clean_block` 通过参数接收这个字典：

```python
# media.py — 下载上传，返回结果
media_results = process_embedded_files(blocks, tgt_doc_id, sp, tp)
# → {"block_id_1": MediaResult(success=True, name="file.zip", ...)}

# block_api.py — 接收结果，决定占位符
clean_block(block, media_results)
# → 查 media_results[block_id]，不再读 block["_file_inserted"]
```

数据流是单向的：`media.py → MediaResult → block_api.py`。

### 先建立阅读坐标

如果你是第一次看代码，推荐先建立下面这 5 层心智模型：

1. `common.py`：公共基础设施
  负责日志、异常、飞书 API / CLI 调用、state 读写。
2. `media.py`：媒体资源迁移层
  只关心"下载 → 上传 → 返回 MediaResult"，不修改 blocks（图片除外，因为 write 需要新 token）。
3. `block_api.py`：Block 操作层
  只关心 blocks 的读取、清洗、写入。清洗时接收 MediaResult 决定占位符内容。
4. `strategies.py`：迁移策略层
  按类型分发，编排"读 → 迁媒体 → 写 blocks → 后处理 → 挂知识库"的完整流程。
5. `post_process.py`：后处理层
  迁移完成后的二次处理：嵌入表格数据复制、占位符文本统一。

你可以把这套系统理解成一句话：

> `migrate/wiki_commands.py` 负责组织任务（scan/run/verify/precheck/report），`strategies.py` 负责选路和编排流程，`media.py` 搬运资源，`block_api.py` 搬运内容，`post_process.py` 做收尾清理（资源复位），`common.py` 提供所有公共能力。

### `state` 是这套系统的骨架

整个项目不是"函数驱动"，而是**state 驱动**。无论是全量迁移、单节点迁移、断点续传，还是后续修链接 / 修提及，都是围绕同一份 state 展开。

`migration-state.json` 的关键字段：

- `meta`：源/目标组织信息、目标空间、挂载根节点等全局上下文
- `nodes`：每个待迁移节点的一条任务记录（类型、标题、父节点、状态、错误信息等）
- `node_mapping`：源 `node_token` 到目标 `node_token` 的映射
- `obj_mapping`：源 `obj_token` 到目标 `obj_token` 的映射
- `stats`：当前 state 的汇总统计

### 单文档迁移的推荐讲法

如果是给导师展示，最稳的讲法是只讲**单文档 docx 迁移**这条主链路。整个流程在 `strategies.py` 的 `migrate_docx()` 中一目了然：

```text
migrate_docx() (strategies.py)
  1. read_docx_blocks()          → 读取源文档所有 blocks    [block_api.py]
  2. 含图片？→ 走 export/import 回退路径
  3. 创建目标空白 docx
  4. _migrate_media_resources()  → 迁移媒体资源             [media.py]
     ├─ process_images()         → 返回 MediaResult
     ├─ process_embedded_files() → 返回 MediaResult
     ├─ process_embedded_sheets()→ 返回 MediaResult
     └─ process_boards()         → 返回 MediaResult
  5. _preprocess_blocks_for_write() → 文件信息冒泡（bt=23→bt=33/bt=2）
  6. write_blocks_bfs()          → BFS 逐层写入 blocks      [block_api.py]
     └─ clean_block(block, media_results) → 清洗 + 生成占位符
  7. post_migrate_embed_sheets() → 插入表格 + 复制数据       [post_process.py]
  8. move_to_wiki()              → 挂入目标知识库
```

如果是 export/import 回退路径（含图片的文档）：

```text
_migrate_docx_export_import()
  1. export docx → import docx
  2. process_embedded_sheets() + post_migrate_embed_sheets()  → 嵌入表格
  3. post_migrate_fix_placeholders()  → 统一占位符格式       [post_process.py]
  4. process_embedded_files()         → 上传文件附件         [media.py]
  5. move_to_wiki()
```

这条链路同时覆盖了：

- 入口和调度：`wiki-migrate.py`（薄）+ `migrate/wiki_commands.py`（实现）
- 类型分发：`strategies.py`
- 媒体迁移：`media.py`
- docx 内容迁移：`block_api.py`
- 后处理：`post_process.py`
- 状态推进：`common.py`

---

## 使用流程

### 1. 扫描源知识库

```bash
python3 wiki-migrate.py scan \
  --source-profile seewo \
  --target-profile xupt \
  --source-space <源空间ID> \
  --target-space <目标空间ID> \
  --state migration-state.json
```

生成 `migration-state.json`，记录所有节点信息和迁移状态。

如果已有节点列表 JSON，可用 `--from-file nodes.json` 跳过 API 扫描。列表里可以只放**一个节点**，不必扫全库。

可选：`scan` 时加 `--target-root <目标父节点 node_token>`，指定迁移内容在目标知识库中的挂载父节点。

### 1b. 只迁单个节点（不必全量 scan）

有三种常见方式，按场景选用即可。

**方式 A：`migrate-one`（推荐，无需预先有 state）**

```bash
python3 wiki-migrate.py migrate-one \
  --source-profile seewo \
  --target-profile xupt \
  --target-space <目标知识空间ID> \
  --node-token <源节点 node_token> \
  --state single-doc.json \
  --target-parent <目标知识库父节点 node_token>
```

**方式 B：`scan --from-file` 再 `run --node-token`**

```bash
python3 wiki-migrate.py scan ... --from-file one-node.json --state migration-state.json
python3 wiki-migrate.py run --state migration-state.json --node-token <该节点的 node_token>
```

**方式 C：手工维护最小 state**

自行编写 state JSON，再执行 `run --node-token ...`。

### 2. 预检报告（推荐）

```bash
python3 wiki-migrate.py precheck --state migration-state.json
```

### 3. 执行迁移

```bash
python3 wiki-migrate.py run --state migration-state.json

# 仅迁移指定节点
python3 wiki-migrate.py run --state migration-state.json --node-token <源 node_token>
```

- 按节点深度分层迁移，父节点优先
- 自动跳过已完成节点，Ctrl+C 中断后重新运行续传
- 状态文件原子写入（临时文件 + rename）
- 网络错误自动重试（最多 5 次），限流自动重试（最多 3 次）

**迁移策略（按文档类型自动选择）：**

| 类型       | 策略                | 说明                                              |
| -------- | ----------------- | ----------------------------------------------- |
| docx     | Block API 逐块读写    | 保真度最高，含图片时自动降级为 export → import |
| doc      | export → import   | 导出为 docx 再导入                                    |
| sheet    | export → import   | 导出为 xlsx 再导入                                    |
| bitable  | export → import   | 导出为 xlsx 再导入                                    |
| file     | download → upload | 二进制文件直接下载上传                                     |
| mindnote | 创建占位文档            | 后续用 `mindnote-export.py` 替换                     |
| slides   | 创建占位文档            | 暂不支持自动迁移                                        |
| shortcut | 最后批量处理            | 根据 token 映射自动创建快捷方式                              |

**Block API 迁移处理的内容类型：**

| 内容           | block_type   | 处理方式                                    |
| ------------ | ------------ | --------------------------------------- |
| 文字/标题/列表/代码块 | 2-9,12-14,17 | 完整保留                                    |
| 图片           | 27           | 下载→上传→替换 token（有图片时整篇降级为 export/import） |
| 原生表格         | 31,32        | 完整保留（含单元格内容）                            |
| Grid 分栏布局    | 24,25        | 完整保留                                    |
| Callout 高亮块  | 19           | 完整保留（删除飞书自动创建的空子节点后重建）                  |
| 引用容器         | 34           | 完整保留                                    |
| 嵌入文件附件       | 23           | 下载→上传，交错写入原位置附近（Block API 路径）；export/import 路径追加到末尾 |
| 嵌入电子表格视图     | 30           | export→import sheet→复位到原位置并复制数据（Block API 唯一化定位）  |
| 画板/白板        | 43           | 下载缩略图→交错写入原位置附近，原位置留占位符                     |
| 嵌入多维表格       | 18           | 原位置生成占位提示（跨组织不可用）                       |
| 嵌入思维导图       | 29           | 原位置生成占位提示（API 无法导出图片）                   |
| @用户提及        | —            | 转为 `@open_id` 文本，由 fix-mentions.py 修复   |
| @文档提及        | —            | 转为带超链接的标题文本，由 fix-links.py 修复           |
| 未知类型         | 其他           | 生成 `[不支持的内容类型]` 占位                      |

**占位符格式统一：**

无论是 Block API 路径还是 export/import 回退路径，迁移后的占位符格式一致。Block API 路径下嵌入文件/表格/画板会自动复位到原位置附近（交错写入 + Block API 唯一化定位），占位符会被删除或保留为参考：

- `[嵌入电子表格: {token}]` — Block API 路径：自动复位到原位置（占位符删除）；export/import 路径：追加到末尾
- `[嵌入文件: {name}]` — Block API 路径：交错写入原位置附近（占位符删除）；export/import 路径：追加到末尾
- `[画板: {token}] (缩略图已迁移到文档末尾，需手动调整位置)` — Block API 路径已交错写入
- `[嵌入多维表格: {token}] (跨组织不可用，需手动重新嵌入)`
- `[嵌入思维导图: {token}] (跨组织不可用，需手动迁移)`

### 4. 验证迁移结果

```bash
python3 wiki-migrate.py verify --state migration-state.json
# 全量验证（默认只抽样 10 个）
python3 wiki-migrate.py verify --state migration-state.json --all
```

### 4b. 生成迁移报告

```bash
python3 wiki-migrate.py report --state migration-state.json \
  --source-domain xxx.feishu.cn \
  --target-domain yyy.feishu.cn
```

生成 Markdown 报告，包含：完全迁移 / 有占位符（需手动检查）/ 迁移失败 / 占位文档（mindnote/slides），每个文档附源/目标链接。

### 5. 迁移思维导图（mindnote）

```bash
# 登录源组织和目标组织
python3 mindnote-export.py login --profile seewo
python3 mindnote-export.py login --profile xupt

# 一键迁移所有思维导图
python3 mindnote-export.py transfer --state migration-state.json --all
```

### 6. 修复文档链接

```bash
# 扫描并修复（替换源 token 为目标 token + 替换域名）
python3 fix-links.py all --state migration-state.json \
  --target-domain yyy.feishu.cn

# 仅扫描不修改
python3 fix-links.py all --state migration-state.json --dry-run
```

### 7. 修复 @提及

```bash
python3 fix-mentions.py all --state migration-state.json --fix
```

### 8. 迁移评论

```bash
python3 migrate-comments.py all --state migration-state.json [--dry-run]
```

### 9. 扫描嵌入内容（辅助工具）

```bash
python3 scan-embeds.py --profile seewo --space <源空间ID> --workers 8
```

---

## 迁移能力边界

### 可完整迁移

| 内容                  | 方式                  |
| ------------------- | ------------------- |
| docx 文字/标题/列表/代码块   | Block API           |
| docx 图片             | 下载→上传→替换 token（export/import 路径保留原位置）|
| docx 原生表格           | Block API           |
| docx Grid 分栏布局      | Block API           |
| docx Callout / 引用容器 | Block API           |
| docx 嵌入文件附件         | 下载→上传（两条路径均支持）|
| docx 嵌入电子表格         | export→import sheet + 数据复制 |
| docx 画板缩略图          | 下载缩略图→上传（Block API 路径）；export/import 路径飞书自动转图片 |
| 文档间链接               | fix-links.py 修复     |
| @用户提及               | fix-mentions.py 修复  |
| 评论（含锚定）             | migrate-comments.py |
| file 附件             | 下载→上传               |

### 降级迁移

| 内容                  | 说明                            |
| ------------------- | ----------------------------- |
| 嵌入电子表格视图            | 数据完整迁移，自动复位到原位置（Block API 路径）；export/import 路径在文档末尾 |
| 嵌入文件附件              | 文件完整迁移，交错写入原位置附近（Block API 路径）；export/import 路径在文档末尾 |
| 画板缩略图               | 交错写入原位置附近（Block API 路径）        |
| @文档提及 (mention_doc) | 转为超链接文本                     |
| doc 旧格式             | 导出为 docx 再导入                  |
| sheet               | 导出为 xlsx 导入，丢失公式/图表       |
| bitable             | 导出为 xlsx 导入，丢失视图/自动化      |

### 无法自动迁移

| 内容                    | 原因                  | 处理          |
| --------------------- | ------------------- | ----------- |
| 嵌入多维表格                | 跨组织 token 不可用       | 生成占位提示      |
| slides 幻灯片            | 无导出/导入 API          | 生成占位文档      |
| 嵌入思维导图                | API 无法导出图片          | 生成占位提示      |
| sheet 公式/图表           | xlsx 导入不保留          | 事后手动处理      |
| bitable 视图/自动化        | xlsx 导入不保留          | 事后手动处理      |

---

## 常见问题

**中断了怎么办？**
直接重新运行 `run`，自动从上次位置继续。

**某些节点失败了？**
查看 state 文件中失败节点的 `error` 字段，修复后重新运行即可重试。

**目标知识库需要准备什么？**
创建一个空的知识库空间，拿到 space_id 即可。

**支持增量迁移吗？**
支持。重新 `scan` 后运行，已完成的节点自动跳过。

## 生成文件

```
<项目根目录>/
├── migration-state.json            ← 核心状态文件
├── migration-state-precheck.json   ← 预检报告
├── migration-state-report.md       ← 迁移报告（Markdown）
├── migration-state-comments.json   ← 评论迁移状态
└── .workdir/                       ← 工作目录（已加入 .gitignore）
    ├── mindnotes/                  ← 思维导图缓存
    ├── session_<profile>.json      ← 浏览器登录态
    ├── embed_sheet_<token>/        ← 嵌入 sheet 临时文件（用完自动清理）
    ├── node_<token>/               ← export/import 临时文件（用完自动清理）
    └── dl_<token>                  ← file 下载临时文件（用完自动清理）
```
