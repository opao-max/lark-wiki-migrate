# 飞书跨组织 wiki 迁移 — 测试发现总结

> 测试时间：2026-04-27
> 目标文档：xupt / WEmwdvqmUoICf6xjxjjc518jnRb
> 源文档：seewo / M2kkdji0coJ7Dpx5V3Fcm9rBnde（含 19 张图片，业务建模开发流程相关）

## 一、推翻的旧结论

| 旧认知（评审材料里写的） | 实测真相 |
|---|---|
| Block API 不能创建 board(43) 块 | ❌ **能创建**。传 `{board: {}}` 飞书自动新建空白画板，返回新 token |
| Block API 不能创建 sheet(30) 块 | ❌ **能创建**。传 `{sheet: {row_size, column_size}}` 飞书自动新建空白 sheet |
| Block API 不能创建 image(27) 块 | ❌ **能创建空块**（`{image: {}}`），后续走 upload + PATCH 流程绑定图片 |
| 跨租户 token 失效，所以图片必须走 export/import | ⚠️ 部分对——根因更精准：见下文「真正的约束」 |
| 思维导图 mindnote(29) 不能创建 | ✅ 真不能。1770029 `block not support to create` |

## 二、真正的约束（评审会要讲的核心）

### 1. 嵌入资源块不允许"引用已有 token"

`board / sheet / bitable / mindnote` 这些"嵌入资源块"，传 `{token: <已有>}` 全部 1770001。
飞书规则是：**Block API 创建即创建一个新的空白底层资源**，不允许把 docx 块绑定到已存在资源。

**对项目影响**：
- 嵌入电子表格的迁移路径只能是：① children API 建空 sheet（拿新 token）→ ② 用 sheets v2 复制源数据
- "先 import 一个 sheet → 把它嵌入 docx" 这条路**走不通**
- 项目当前 `post_process` 的 `<sheet/>` 占位 + insert_after 思路方向对，但**可以简化**：直接用 children API 建带 row/col size 的 sheet block，省去占位符 + 删占位符两步

### 2. image.token 与一个特定 image block 绑定，不可解、不可复用

完整流程（来自 `lark-cli docs +media-insert --dry-run` 的 4 步揭示）：
1. POST `/blocks/{doc}/children` 创建空 image block → `new_bid`
2. POST `/drive/v1/medias/upload_all` 带 `parent_type=docx_image, parent_node=<new_bid>` 上传 → `file_token`（已绑定 new_bid）
3. PATCH `/blocks/batch_update` 用 `replace_image: {token}` 绑定到 new_bid

**实测**：
- step 3 PATCH 到**同一个 block** + 同一 token = 成功（code 0）
- step 3 PATCH 到**另一个 image block** + 这个 token = **1770013 relation mismatch**

**对项目影响**：
- 不能"上传一次图片，复用 token 给多个 block"
- 每张图必须独立完成 4 步循环
- `media.py` 已经实现这个 4 步循环（用的就是 `media-insert`），但 **`strategies.py:222-226` 检测到图片就强制 export/import 降级**，导致 media.py 那段代码**永远跑不到**——这是个项目内部的死代码 + 错误降级

### 3. lark-cli 工具链的限制

- `lark-cli api` 不支持 multipart 文件上传（`/medias/upload_all` 不能直接调）
- 必须依赖 `lark-cli docs +media-insert`，而它内部把 image 块**追加到文档末尾**
- 之后如果要"放到原位"，PATCH 只能改 token 不改 parent/index → 位置丢失
- 这是项目走 `_write_root_kids_interleaved`「交错写入」策略的根本原因

### 4. descendant API 的真实能力（项目从未使用）

URI：`POST /open-apis/docx/v1/documents/:doc/blocks/:parent/descendant`
- body 携带 `descendants[]`（扁平 Block 列表）+ `children_id` + `index`
- 一次提交完整子树（表格 + 单元格 + 单元格内文本/资源）

**实测**（test6）：
- ✅ 测试 A（表格 + cell + 文本）：1770041 schema mismatch（DSL 还需打磨）
- ✅ 测试 B 出现 **关键突破**：descendant **可以在 table_cell 内塞 board(43) 块**！
  - 实测目标文档里出现了 `table(31) → table_cell(32) → board(43)` 的真实嵌套
  - 而 children API 在容器内只能塞普通块——这是质的差异
- ❌ 测试 B 中 sheet(30) 在 cell 内仍然 1770001——服务端规则对 sheet 仍有限制

**对项目影响**：
- "嵌套画板只能放文档末尾"的限制，**用 descendant 可以突破**
- 项目当前对嵌套画板的处理（`_get_interleaved_mr` 只处理根级）有改进空间

## 三、跑完测试后建议的工程改动

按价值由高到低：

1. **删除/重构 strategies.py:222-226 的图片强制降级**
   - media.py 已实现完整 4 步上传，去掉这个 `return _migrate_docx_export_import(node, state)` 的硬降级
   - 实测含 19 图的 docx 用 Block API 应该能跑，比 export/import 保真度高

2. **嵌入电子表格简化为单步 children API 创建**
   - 当前：占位符 → BFS 写入 → post_process insert_after `<sheet/>` → 删占位符
   - 改为：BFS 时直接 `children API` 创建 `{sheet: {row_size, column_size}}` 拿新 token → sheets v2 复制数据
   - 减少 2 个 API 往返 + 删除 post_process 的复杂占位符匹配代码

3. **嵌套画板用 descendant API**
   - 当前：嵌套位置画板 → 直接占位文本（`[画板: ...]`）
   - 改为：在 BFS 写入时，遇到嵌套 board 用 descendant 一次性创建到 cell 内
   - 评审会能演示一个突破

4. **修复 test1_block_create.py 的误测**
   - 现有 test1 给 board 不传 `{board: {}}`、给 sheet 传 `fake` token，导致全员 1770001
   - 误导团队认为这些块不能创建——其实是 payload 错了
   - 这是项目记忆里写的"禁忌"的源头，要在评审会上澄清

## 四、还没解决的问题

- **mindnote 真的不能创建** —— 1770029 是服务端硬性限制
  - SDK 中 import_task `type` 列表包含 mindnote，但需要 .xmind/.opml/.mm 等可识别格式
  - 源租户 mindnote 没有 export API（test3 已证明），所以拿不到这种文件
  - 结论：mindnote 仍然只能手动迁移
- **bitable 嵌入** —— sheet/board 都能在 cell 内（descendant）但 bitable 还没测
- **slides** —— 类似 mindnote，没 API 路径

## 五、新生成的测试文件

| 文件 | 验证 | 状态 |
|---|---|---|
| `tools/test1_block_create.py` | block_type 创建限制（**有 bug，需修**） | 旧 |
| `tools/test1_image_block.py` | image block 创建（伪 token） | 旧 |
| `tools/test2_nested_file.py` | 表格内嵌套文件迁移 | 旧 |
| `tools/test3_mindnote_export.py` | mindnote export 不支持 | 旧 |
| `tools/test4_image_block_via_target_token.py` | image token 与 block 的绑定关系 | **新（已跑）** |
| `tools/test5_sheet_block_via_target_token.py` | sheet block 必须新建不能引用 | **新（已跑）** |
| `tools/test6_descendant_api.py` | descendant API + cell 内塞 board | **新（已跑，关键发现）** |
| `tools/test7_descendant_cell_embeds.py` | cell 内嵌 5 类资源块的边界 | **新 2026-04-28** |
| `tools/test8_import_task_point.py` | import_task `point` 是否能直挂 wiki | **新 2026-04-28** |
| `tools/test9_mindnote_import.py` | 自造 .xmind 走 import → mindnote | **新 2026-04-28** |
| `tools/test10_curl_multipart.py` | multipart 自定义 parent_node 可行性调查 | **新 2026-04-28** |
| `tools/test11_whiteboard_download.py` | whiteboard `/download_as_image` 高清渲染 | **新 2026-04-28** |
| `tools/test12_descendant_text_in_cell.py` | descendant cell 内文本/段落 schema 排查 | **新 2026-04-28** |

---

## 六、第二轮测试（2026-04-28，基于 SDK + lark-cli 联合调查）

### 关键修正 / 新增结论

**1. cell 内可嵌入哪些块（test7+test12 完整边界）**

| bt | 类型 | cell 内 descendant | 说明 |
|----|------|---------|------|
| 2  | text | ✓ | 必须有 elements（裸 text_run 也行） |
| 4  | heading2 | ✓ | 同 text |
| 27 | image | ✓ | `{image:{}}` 即可 |
| 43 | board | ✓ | `{board:{}}` 即可（test6 已证） |
| 30 | sheet | ✗ 1770030 | "invalid parent children relation"，服务端规则 |
| 18 | bitable | ✗ 1770030 / 1770001 | 同上 |
| 23 | file | ✗ 1770030 / 1770001 | 同上 |
| 29 | mindnote | ✗ 1770029 | 服务端硬限制（与 children 一致） |

→ **嵌套画板 / 嵌套图片**可以用 descendant API 一次性创建到 cell 内，
  突破"只能追加文档末尾"。
→ 嵌套 sheet/bitable/file 仍然只能落根级（或占位文本）。
→ cell 必须至少有一个 child（V3 失败：1770041 schema mismatch），
  空 cell 不能写。

**2. import_task `point` 字段不支持 wiki 直挂（test8）**

服务端明确返回 `point.mount_type is optional, options: [1]` —— **只支持 mount_type=1 挂"我的空间"根目录**，
SDK 字段虽然是裸 int 但服务端枚举只有 1。

→ 项目里所有 `move_docs_to_wiki` 调用都**不能省**。
→ findings.md 旧表里"调研 point 是否能省 move"的悬念关闭。

**3. mindnote 通过 import_task 也不可达（test9）**

实测三种 type（mindnote / mindmap / mind）+ ext=xmind 的组合，全部 1069904 invalid param。
→ mindnote 在 OpenAPI 全栈无创建路径（既不能 Block API 创建，也不能 import_task 创建）。
→ "mindnote 只能手动迁移"是终极结论。

**4. lark-cli 唯一限制是 multipart 隐藏（test10）**

dry-run 揭示 `media-insert` 4 步内部完全是普通 API：
- step 2 写死 `index = <children_len>`（永远末尾）
- step 3 用 multipart 上传，parent_node = step 2 的 block_id

实测 children API 接受任意 index ∈ [0, children_len]——**位置在飞书 API 层不是约束，是 lark-cli 包装的限制**。

→ 真正的"原位插图"改造路径：写独立 Python 客户端走 multipart，
  绕开 lark-cli。需要单独 oauth 流程拿 user_token（lark-cli keychain
  加密未破解）。

**5. whiteboard `/download_as_image` 是画板高保真新路径（test11）**

实测同一个画板：
- `lark-cli docs +media-download <token>` → **HTTP 404 失败**
- `GET /board/v1/whiteboards/{id}/download_as_image` → **2560×2560 JPEG，44KB**

→ 项目 `media.py:process_boards` 当前用 media-download 的方式不仅低清，**部分画板根本下载不到**。
→ 改用 whiteboard download_as_image 既高清又可靠。这是评审会能演示的明显改进。

### 第二轮工程改动建议（追加在原"三、"之后）

5. **process_boards 改用 whiteboard/download_as_image**
   - 当前 lark-cli docs +media-download 在某些画板上 HTTP 404
   - 改为 `GET /board/v1/whiteboards/{token}/download_as_image` 直接拿 2560 高清 JPEG
   - 入口：`media.py:process_boards`

6. **嵌套位置的画板/图片用 descendant 写入**（findings 第 3 条已提，本轮新增图片）
   - 不仅 board，cell 内的 image 也能 descendant 写入
   - 入口：`block_api.py:_get_interleaved_mr` + `clean_block` bt=27/43 分支

### 已关闭的悬念

- ~~"point 能否省 move_docs_to_wiki"~~ → 不能，mount_type 只支持 1
- ~~"mindnote .xmind import 是否吃"~~ → 不吃，1069904
- ~~"descendant Test A 1770041 是 DSL 问题吗"~~ → 不是 DSL，是 cell 必须有 child
- ~~"sheet/bitable/file 在 cell 内能否突破"~~ → 服务端规则，不能突破

### 仍未解决

- 直接 multipart 上传（绕开 lark-cli）需独立 Python oauth 客户端，未实施
- bitable 嵌入跨租户复制（沿用 export/import xlsx，但会丢公式/视图）
- slides（无 API 路径，与 mindnote 同列）

