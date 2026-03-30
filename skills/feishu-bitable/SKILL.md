---
name: feishu-bitable
description: 创建和操作飞书多维表格（Feishu Bitable）。当用户说"写个多维表格"、"创建bitable"、"飞书数据库"、"feishu bitable"等关键词时触发。
license: MIT
---

# 飞书多维表格（Feishu Bitable API）

通过飞书 Bitable API 创建和操作多维表格（类 Airtable），支持多种字段类型、CRUD 操作、筛选查询等。

## 触发条件

- "写个多维表格"、"创建 bitable"
- "飞书数据库"、"feishu bitable"
- "数据写到多维表格里"

## 初始化

```python
import os, lark_oapi as lark
from lark_oapi.api.bitable.v1 import *

client = lark.Client.builder() \
    .app_id(os.environ['FEISHU_APP_ID_JARVIS']) \
    .app_secret(os.environ['FEISHU_APP_SECRET_JARVIS']) \
    .domain(lark.FEISHU_DOMAIN).build()
```

## 创建 Bitable App

```python
req = CreateAppRequest.builder() \
    .request_body(ReqApp.builder()
        .name("我的多维表格")
        .folder_token("")  # 空 = 根目录
        .build()).build()
resp = client.bitable.v1.app.create(req)
app_token = resp.data.app.app_token
url = resp.data.app.url
```

## 创建数据表（带字段定义）

```python
fields = [
    AppTableCreateHeader.builder().field_name("名称").type(1).build(),      # 文本
    AppTableCreateHeader.builder().field_name("状态").type(3).build(),      # 单选
    AppTableCreateHeader.builder().field_name("标签").type(4).build(),      # 多选
    AppTableCreateHeader.builder().field_name("日期").type(5).build(),      # 日期
    AppTableCreateHeader.builder().field_name("数值").type(2).build(),      # 数字
    AppTableCreateHeader.builder().field_name("链接").type(15).build(),     # 超链接
    AppTableCreateHeader.builder().field_name("完成").type(7).build(),      # 复选框
]
req = CreateAppTableRequest.builder() \
    .app_token(app_token) \
    .request_body(CreateAppTableRequestBody.builder()
        .table(ReqTable.builder()
            .name("任务列表")
            .default_view_name("全部")
            .fields(fields).build())
        .build()).build()
resp = client.bitable.v1.app_table.create(req)
table_id = resp.data.table_id
```

## 字段类型速查

| type | 类型 | 值格式 | 示例 |
|------|------|--------|------|
| 1 | 文本 | `str` | `"Hello"` |
| 2 | 数字 | `int/float` | `42`, `3.14` |
| 3 | 单选 | `str` | `"已完成"` |
| 4 | 多选 | `list[str]` | `["TPU", "训练"]` |
| 5 | 日期 | `int` (ms) | `1710000000000` |
| 7 | 复选框 | `bool` | `True` |
| 11 | 人员 | `list[dict]` | `[{"id": "ou_xxx"}]` |
| 13 | 电话 | `str` | `"+86-10-12345"` |
| 15 | 超链接 | `dict` | `{"text": "Google", "link": "https://..."}` |
| 17 | 附件 | `list[dict]` | `[{"file_token": "xxx"}]` |
| 20 | 公式 | 只读 | API 不可写 |

**注意**: 评分字段 (type=22) API 写入可能报 `WrongRequestBody`，建议用数字字段替代。

## 添加记录

### 单条

```python
record = AppTableRecord.builder().fields({
    "名称": "部署推理服务",
    "状态": "进行中",
    "标签": ["GPU", "推理"],
    "日期": int(time.time() * 1000),
    "数值": 75,
    "链接": {"text": "文档", "link": "https://example.com"},
    "完成": False,
}).build()
req = CreateAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id) \
    .request_body(record).build()
resp = client.bitable.v1.app_table_record.create(req)
record_id = resp.data.record.record_id
```

### 批量

```python
records = [AppTableRecord.builder().fields(r).build() for r in records_data]
req = BatchCreateAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id) \
    .request_body(BatchCreateAppTableRecordRequestBody.builder()
        .records(records).build()).build()
resp = client.bitable.v1.app_table_record.batch_create(req)
```

## 查询记录

### 列出所有

```python
req = ListAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id) \
    .page_size(100).build()
resp = client.bitable.v1.app_table_record.list(req)
for item in resp.data.items:
    print(item.record_id, item.fields)
```

### 条件筛选

```python
req = ListAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id) \
    .filter('CurrentValue.[状态] = "进行中"') \
    .build()
```

### 筛选语法

```
CurrentValue.[字段名] = "值"              # 等于
CurrentValue.[字段名] != "值"             # 不等于
CurrentValue.[数值字段] > 50              # 大于
CurrentValue.[字段名].contains("关键词")   # 包含
AND(条件1, 条件2)                         # 且
OR(条件1, 条件2)                          # 或
```

## 更新记录

```python
req = UpdateAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id).record_id(record_id) \
    .request_body(AppTableRecord.builder()
        .fields({"状态": "已完成", "完成": True}).build()).build()
resp = client.bitable.v1.app_table_record.update(req)
```

## 删除记录

```python
req = DeleteAppTableRecordRequest.builder() \
    .app_token(app_token).table_id(table_id).record_id(record_id).build()
client.bitable.v1.app_table_record.delete(req)
```

## 管理数据表

### 列出所有表

```python
req = ListAppTableRequest.builder().app_token(app_token).build()
resp = client.bitable.v1.app_table.list(req)
for t in resp.data.items:
    print(f"{t.table_id}: {t.name}")
```

### 删除表

```python
req = DeleteAppTableRequest.builder() \
    .app_token(app_token).table_id(table_id).build()
client.bitable.v1.app_table.delete(req)
```

## 实际应用场景

### Bot Inbox 通信

```python
# dispatch.py 用 Bitable 做 bot 间消息队列
record = AppTableRecord.builder().fields({
    "task_id": f"dispatch-{timestamp}",
    "from": "jarvis",
    "instruction": "hostname",
    "status": "pending",
}).build()
```

### 项目管理看板

```python
# 创建带状态流转的数据表
fields = [
    AppTableCreateHeader.builder().field_name("任务").type(1).build(),
    AppTableCreateHeader.builder().field_name("状态").type(3).build(),  # 待办/进行中/已完成
    AppTableCreateHeader.builder().field_name("负责人").type(11).build(),
    AppTableCreateHeader.builder().field_name("截止日期").type(5).build(),
    AppTableCreateHeader.builder().field_name("优先级").type(3).build(),  # P0/P1/P2
]
```

## 注意事项

- **App Token vs Table ID**: app_token 标识整个多维表格应用，table_id 标识其中一张数据表
- **日期格式**: Unix 毫秒时间戳（`int(time.time() * 1000)`），不是秒
- **单选/多选自动创建**: 写入未定义的选项值时，Bitable 会自动创建该选项
- **分页**: `list` 默认返回 20 条，最多 500 条。用 `page_token` 翻页
- **Rate limit**: 约 100 次/分钟
- **权限**: App 需要 `bitable:app` 权限
- **URL 格式**: `https://xxx.feishu.cn/base/{app_token}`

## 完整工作流程

1. **创建 App** → `CreateAppRequest` → 拿到 `app_token`
2. **创建数据表** → `CreateAppTableRequest` → 定义字段和类型
3. **写入数据** → `CreateAppTableRecordRequest` / `BatchCreate`
4. **查询/筛选** → `ListAppTableRecordRequest` + `filter`
5. **更新/删除** → `UpdateAppTableRecordRequest` / `DeleteAppTableRecordRequest`
6. **返回链接** → `https://xxx.feishu.cn/base/{app_token}`
