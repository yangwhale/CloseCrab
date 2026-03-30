---
name: feishu-sheet
description: 创建和操作飞书电子表格（Feishu Spreadsheet）。当用户说"写个飞书表格"、"创建电子表格"、"feishu sheet"等关键词时触发。
license: MIT
---

# 飞书电子表格（Feishu Sheets API）

通过飞书 Sheets API 创建和操作电子表格，支持写值、样式、公式、合并单元格、多 Sheet、数据验证等。

## 触发条件

- "写个飞书表格"、"创建电子表格"
- "feishu sheet"、"飞书 spreadsheet"
- "数据写到飞书表格里"

## API 结构

飞书 Sheets 有两套 API：
- **v3 SDK** (`lark_oapi.api.sheets.v3`): 创建表格、查询 sheet 信息、筛选等
- **v2 HTTP** (`/open-apis/sheets/v2/spreadsheets`): 写值、样式、合并、公式等

实践中两套配合使用：v3 创建，v2 操作数据。

## 初始化

```python
import os, json, requests
import lark_oapi as lark
from lark_oapi.api.sheets.v3 import *

client = lark.Client.builder() \
    .app_id(os.environ['FEISHU_APP_ID_JARVIS']) \
    .app_secret(os.environ['FEISHU_APP_SECRET_JARVIS']) \
    .domain(lark.FEISHU_DOMAIN).build()

def get_token():
    """获取 tenant_access_token 用于 v2 HTTP API。"""
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", json={
        "app_id": os.environ['FEISHU_APP_ID_JARVIS'],
        "app_secret": os.environ['FEISHU_APP_SECRET_JARVIS'],
    })
    return r.json()['tenant_access_token']
```

## 创建电子表格（v3 SDK）

```python
req = CreateSpreadsheetRequest.builder() \
    .request_body(SpreadsheetBuilder().title("表格标题").build()).build()
resp = client.sheets.v3.spreadsheet.create(req)
ss_token = resp.data.spreadsheet.spreadsheet_token
url = resp.data.spreadsheet.url
```

## 查询 Sheet 列表（v3 SDK）

```python
req = QuerySpreadsheetSheetRequest.builder().spreadsheet_token(ss_token).build()
resp = client.sheets.v3.spreadsheet_sheet.query(req)
for s in resp.data.sheets:
    print(f"sheet_id={s.sheet_id} title={s.title}")
```

## 写入数据（v2 HTTP）

```python
BASE = "https://open.feishu.cn/open-apis/sheets/v2/spreadsheets"
headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

data = {
    "valueRange": {
        "range": f"{sheet_id}!A1:C3",  # sheet_id!起始:结束
        "values": [
            ["标题1", "标题2", "标题3"],
            ["值1", 42, 3.14],
            ["值4", 100, 0.856],
        ]
    }
}
r = requests.put(f"{BASE}/{ss_token}/values", headers=headers, json=data)
```

### 支持的值类型

- **字符串**: `"文本"`
- **整数**: `42`
- **浮点数**: `3.14`
- **公式**: `"=SUM(A1:A10)"`, `"=AVERAGE(B2:B11)"`
- **空值**: `""` 或 `None`
- **不支持**: Python `bool`（用字符串 `"TRUE"/"FALSE"` 代替）

## 设置样式（v2 HTTP）

```python
style_data = {
    "data": [
        {
            "ranges": [f"{sheet_id}!A1:F1"],
            "style": {
                "font_size": 12,         # 字号
                "bold": True,            # 粗体
                "italic": True,          # 斜体
                "foreColor": "#FFFFFF",  # 文字颜色（hex）
                "backColor": "#4C6EF5", # 背景色（hex）
                "hAlign": 0,            # 水平对齐：0=左 1=中 2=右
                "vAlign": 0,            # 垂直对齐：0=上 1=中 2=下
                "strikethrough": True,   # 删除线
                "underline": True,       # 下划线
            }
        }
    ]
}
r = requests.put(f"{BASE}/{ss_token}/styles_batch_update", headers=headers, json=style_data)
```

### 一次设置多个范围

`data` 数组中可以放多个 `{ranges, style}` 对象，一次 API 调用设置多个区域的样式。

## 合并单元格（v2 HTTP）

```python
merge_data = {
    "range": f"{sheet_id}!A1:D1",
    "mergeType": "MERGE_ALL"  # MERGE_ALL | MERGE_ROWS | MERGE_COLUMNS
}
r = requests.post(f"{BASE}/{ss_token}/merge_cells", headers=headers, json=merge_data)
```

## 管理 Sheet（v2 HTTP）

### 新增 Sheet

```python
data = {
    "requests": [{
        "addSheet": {
            "properties": {
                "title": "新工作表",
                "index": 1  # 位置
            }
        }
    }]
}
r = requests.post(f"{BASE}/{ss_token}/sheets_batch_update", headers=headers, json=data)
new_sheet_id = r.json()['data']['replies'][0]['addSheet']['properties']['sheetId']
```

### 重命名 Sheet

```python
data = {
    "requests": [{
        "updateSheet": {
            "properties": {
                "sheetId": sheet_id,
                "title": "新名称"
            }
        }
    }]
}
r = requests.post(f"{BASE}/{ss_token}/sheets_batch_update", headers=headers, json=data)
```

### 删除 Sheet

```python
data = {"requests": [{"deleteSheet": {"sheetId": sheet_id}}]}
r = requests.post(f"{BASE}/{ss_token}/sheets_batch_update", headers=headers, json=data)
```

## 设置列宽（v2 HTTP）

```python
data = {
    "dimension": {
        "sheetId": sheet_id,
        "majorDimension": "COLUMNS",
        "startIndex": 3,  # 从 1 开始！0 会报错
        "endIndex": 3
    },
    "dimensionProperties": {
        "fixedSize": 250  # 像素
    }
}
r = requests.put(f"{BASE}/{ss_token}/dimension_range", headers=headers, json=data)
```

**注意**: `startIndex` 必须 > 0，索引从 1 开始，不是 0。

## 公式

公式作为字符串值写入，飞书会自动计算：

```python
values = [["合计", "=SUM(B2:B10)", "=AVERAGE(C2:C10)", "=MAX(D2:D10)"]]
```

常用公式：`SUM`, `AVERAGE`, `COUNT`, `MAX`, `MIN`, `IF`, `VLOOKUP`, `CONCATENATE`

## 斑马纹（交替行颜色）

```python
even_rows = [f"{sheet_id}!A{i}:F{i}" for i in range(2, 12, 2)]
style_data = {
    "data": [{"ranges": even_rows, "style": {"backColor": "#F8F9FA"}}]
}
```

## 常见颜色参考

| 用途 | Hex | 效果 |
|------|-----|------|
| 蓝色表头 | `#4C6EF5` | 专业感 |
| 粉色表头 | `#E64980` | 活泼 |
| 绿色文字 | `#2B8A3E` | 成功/通过 |
| 红色文字 | `#E03131` | 警告/失败 |
| 浅灰背景 | `#F8F9FA` | 斑马纹偶数行 |
| 深灰背景 | `#F1F3F5` | 分区标题行 |

## 注意事项

- **token 有效期**: `tenant_access_token` 有效期 2 小时，长时间操作需要刷新
- **Rate limit**: 约 100 次/分钟，批量操作建议合并 ranges
- **Range 格式**: `{sheet_id}!A1:F10`，sheet_id 是查询返回的短 ID（如 `be5f93`）
- **布尔值**: Python `True/False` 不支持直接写入，用字符串 `"TRUE"/"FALSE"`
- **startIndex**: 列宽/行高 API 的 index 从 1 开始，不是 0
- **URL 格式**: `https://xcn6w52qes4g.feishu.cn/sheets/{spreadsheet_token}`（域名取决于租户）

## 完整工作流程

1. **创建表格** → v3 SDK `CreateSpreadsheetRequest` → 拿到 `spreadsheet_token`
2. **查询 sheet_id** → v3 SDK `QuerySpreadsheetSheetRequest`
3. **获取 token** → HTTP POST `/auth/v3/tenant_access_token/internal`
4. **写数据** → v2 HTTP PUT `/values`
5. **设样式** → v2 HTTP PUT `/styles_batch_update`
6. **合并/列宽等** → v2 HTTP 各端点
7. **返回链接** → `https://xxx.feishu.cn/sheets/{token}`
