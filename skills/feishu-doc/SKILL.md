---
name: feishu-doc
description: 创建飞书文档（Feishu DocX）。当用户说"写个飞书文档"、"创建飞书文档"、"生成飞书doc"、"feishu doc"等关键词时触发。
license: MIT
---

# 飞书文档创建（Feishu DocX API）

通过飞书 DocX API 创建富格式文档，支持文本格式、标题、列表、表格、代码块、Callout、公式等全部原生能力。

## 触发条件

- "写个飞书文档"、"创建飞书文档"
- "生成飞书doc"、"feishu doc"
- "写到飞书文档里"、"输出成飞书文档"

## 前置条件

- 飞书 App 需要 `docx:document` 和 `drive:drive` 权限
- 环境变量已配置：`FEISHU_APP_ID_JARVIS` / `FEISHU_APP_SECRET_JARVIS`（或对应 bot 的 env）

## API 概览

### 1. 创建文档

```python
import os, lark_oapi as lark
from lark_oapi.api.docx.v1 import *

client = lark.Client.builder() \
    .app_id(os.environ['FEISHU_APP_ID_JARVIS']) \
    .app_secret(os.environ['FEISHU_APP_SECRET_JARVIS']) \
    .domain(lark.FEISHU_DOMAIN).build()

req = CreateDocumentRequest.builder() \
    .request_body(CreateDocumentRequestBody.builder()
        .title("文档标题")
        .folder_token("")  # 空字符串 = 根目录
        .build()).build()
resp = client.docx.v1.document.create(req)
doc_id = resp.data.document.document_id
```

### 2. 添加内容块

```python
body = CreateDocumentBlockChildrenRequestBody.builder() \
    .children(blocks)  # List[Block]
    .index(-1)         # -1 = 追加到末尾
    .build()
req = CreateDocumentBlockChildrenRequest.builder() \
    .document_id(doc_id) \
    .block_id(parent_id)      # doc_id = 根级，或容器 block_id
    .document_revision_id(-1) # -1 = 最新版本
    .request_body(body).build()
resp = client.docx.v1.document_block_children.create(req)
```

### 3. 获取子块（表格填充用）

```python
req = GetDocumentBlockChildrenRequest.builder() \
    .document_id(doc_id) \
    .block_id(table_block_id) \
    .build()
resp = client.docx.v1.document_block_children.get(req)
cell_ids = [item.block_id for item in resp.data.items]
```

## Block Types 速查

| block_type | 类型 | Builder setter | 说明 |
|-----------|------|---------------|------|
| 2 | text | `.text()` | 普通文本段落 |
| 3-8 | heading1-6 | `.heading1()` ~ `.heading6()` | 标题 H1-H6 |
| 12 | bullet | `.bullet()` | 无序列表 |
| 13 | ordered | `.ordered()` | 有序列表 |
| 14 | code | `.code()` | 代码块 |
| 15 | quote | `.quote()` | 引用行 |
| 17 | todo | `.todo()` | 任务清单 |
| 19 | callout | `.callout()` | 高亮块（容器） |
| 22 | divider | `.divider({})` | 分割线 |
| 25 | grid | `.grid()` | 多列布局（API 不支持创建） |
| 27 | iframe | `.iframe()` | 内嵌网页 |
| 28 | image | `.image()` | 图片（需先上传获取 token） |
| 31 | table | `.table()` | 表格 |
| 34 | quote_container | `.quote_container({})` | 引用容器（可嵌套） |

## 构建 Block 的工具函数

以下函数可直接复制使用：

### make_text_element — 创建带格式的文本片段

```python
def make_text_element(content, bold=False, italic=False, underline=False,
                      strikethrough=False, inline_code=False,
                      text_color=None, bg_color=None, link=None):
    style_builder = TextElementStyleBuilder()
    if bold: style_builder.bold(True)
    if italic: style_builder.italic(True)
    if underline: style_builder.underline(True)
    if strikethrough: style_builder.strikethrough(True)
    if inline_code: style_builder.inline_code(True)
    if text_color: style_builder.text_color(text_color)     # 1=红 2=橙 3=黄 4=绿 5=蓝 6=紫
    if bg_color: style_builder.background_color(bg_color)   # 同上
    if link: style_builder.link(LinkBuilder().url(link).build())
    return TextElementBuilder() \
        .text_run(TextRunBuilder().content(content)
            .text_element_style(style_builder.build()).build()).build()
```

### make_text_block — 创建文本类 Block

```python
def make_text_block(elements, block_type=2, align=None, style_kwargs=None):
    style_builder = TextStyleBuilder()
    if align: style_builder.align(align)  # 1=左 2=中 3=右
    if style_kwargs:
        if 'language' in style_kwargs: style_builder.language(style_kwargs['language'])
        if 'done' in style_kwargs: style_builder.done(style_kwargs['done'])
    text = TextBuilder().elements(elements).style(style_builder.build()).build()
    bb = BlockBuilder().block_type(block_type)
    type_map = {
        2: 'text', 3: 'heading1', 4: 'heading2', 5: 'heading3',
        6: 'heading4', 7: 'heading5', 8: 'heading6',
        12: 'bullet', 13: 'ordered', 14: 'code', 15: 'quote', 17: 'todo',
    }
    getattr(bb, type_map.get(block_type, 'text'))(text)
    return bb.build()
```

### make_divider — 分割线

```python
def make_divider():
    return BlockBuilder().block_type(22).divider({}).build()
```

## 常用代码块语言 ID

| ID | 语言 | ID | 语言 |
|----|------|----|------|
| 1 | PlainText | 22 | JavaScript |
| 15 | Bash/Shell | 40 | TypeScript |
| 49 | Python | 18 | Java |
| 19 | JSON | 12 | Go |
| 7 | C++ | 56 | YAML |
| 29 | Markdown | 54 | SQL |
| 53 | Rust | 4 | C |

## Callout 高亮块

Callout 是容器块，创建后需要往内部添加子块：

```python
callout = BlockBuilder().block_type(19) \
    .callout(CalloutBuilder()
        .background_color(4)   # 1=红 2=橙 3=黄 4=绿 5=蓝 6=紫
        .border_color(4)
        .emoji_id("bulb")      # 英文 emoji 名称，非 unicode
        .build()).build()
ids = add_blocks([callout])
add_blocks([make_text_block(...)], parent_id=ids[0])  # 填充内容
```

常用 emoji_id: `bulb`, `check`, `warning`, `star`, `crystal_ball`, `rocket`, `fire`, `heart`, `memo`, `pushpin`

## 表格

表格创建后自动生成空 cell，需要查询 cell_ids 再逐个填充：

```python
table = BlockBuilder().block_type(31) \
    .table(TableBuilder()
        .property(TablePropertyBuilder().row_size(3).column_size(4).build())
        .cells([]).build()).build()
table_ids = add_blocks([table])

# 获取 cell block_ids
req = GetDocumentBlockChildrenRequest.builder() \
    .document_id(doc_id).block_id(table_ids[0]).build()
resp = client.docx.v1.document_block_children.get(req)
cell_ids = [item.block_id for item in resp.data.items]
# cells 按行优先排列: [row0col0, row0col1, ..., row1col0, ...]

for idx, cell_id in enumerate(cell_ids):
    row, col = idx // num_cols, idx % num_cols
    add_blocks([make_text_block([make_text_element(data[row][col])])], parent_id=cell_id)
```

## 行内公式（LaTeX）

```python
make_text_block([
    make_text_element("能量公式："),
    TextElementBuilder().equation(
        EquationBuilder().content("E = mc^2").build()
    ).build(),
])
```

## 引用容器

容器块，可嵌套文本、列表等：

```python
qc = BlockBuilder().block_type(34).quote_container({}).build()
qc_ids = add_blocks([qc])
add_blocks([make_text_block(...)], parent_id=qc_ids[0])
```

## 图片

需要先上传文件获取 image token，再创建 image block：

```python
# 1. 上传图片到飞书（使用 drive API 的 upload_media）
# 2. 创建 image block
image_block = BlockBuilder().block_type(28) \
    .image(ImageBuilder().token(image_token).width(800).height(400).build()).build()
```

## 注意事项

- **Rate limit**: 每秒约 5 次 API 调用，批量操作间加 `time.sleep(0.2)`
- **Grid 不支持 API 创建**: `block_type=25` 返回 `block not support to create`，只能在客户端手动添加
- **容器块填充**: Callout (19)、Quote Container (34)、Table Cell (32) 都是容器，创建后需要往 children 添加内容
- **document_revision_id=-1**: 总是使用最新版本，避免并发冲突
- **文档 URL**: `https://bytedance.feishu.cn/docx/{document_id}`

## 工作流程

1. **创建空文档** → 拿到 `document_id`
2. **规划内容结构** → 标题、段落、表格、Callout 等
3. **批量添加块** → 一次 API 调用可加多个同级 block
4. **填充容器** → 表格 cell、callout、quote_container 需要二次填充
5. **返回文档链接** → `https://bytedance.feishu.cn/docx/{document_id}`
