---
name: go-eat
description: 查看 Google 办公室今天吃什么（早餐、午餐、下午茶）。通过 Chrome MCP 访问 go/eat 获取餐厅菜单。当用户说"今天吃什么"、"午饭吃什么"、"看看菜单"、"早餐有什么"、"go/eat"、"食堂"、"饭堂"、"cafeteria"、"what's for lunch"、"menu"等关键词时触发。
---

# go/eat — 办公室餐厅菜单

通过 Chrome DevTools MCP 访问 `go/eat`，查看 Google 办公室当天的餐厅菜单。

## 触发条件

- "今天吃什么"、"午饭吃什么"、"早餐有什么"、"下午茶"
- "看看菜单"、"食堂"、"饭堂"、"cafeteria"
- "go/eat"、"what's for lunch"、"menu"

## 操作步骤

### 1. 导航到 go/eat

```
navigate_page(type="url", url="http://go/eat")
```

页面会 redirect 到 `https://eat.googleplex.com/nearby?meal=now`，默认显示当前时段的菜单。

### 2. 切换餐次（如需要）

页面有 meal selector，可选：
- **Breakfast** — 早餐
- **Lunch** — 午餐
- **Snack/Afternoon** — 下午茶

如果用户问的不是当前时段，需要在 meal selector 中切换。selector 通常是 `listbox "Select meal: today"` 区域。

### 3. 展开餐厅

左侧有餐厅列表，分 OPEN 和 CLOSED 两组。点击 CLOSED 组里的餐厅 tab 可展开。通常需要展开所有餐厅才能看到完整菜单。

### 4. 获取菜单

```
take_snapshot(filePath="/tmp/eat-snapshot.txt")
```

snapshot 通常很大，用脚本提取结构化菜单：

```python
python3 << 'PYEOF'
import re
with open('/tmp/eat-snapshot.txt') as f:
    lines = f.readlines()
for line in lines:
    l = line.strip()
    if 'heading' in l and 'level="3"' in l:
        m = re.search(r'"([^"]+)"', l)
        if m: print(f"\n### {m.group(1)}")
    elif 'heading' in l and 'level="4"' in l:
        m = re.search(r'"([^"]+)"', l)
        if m: print(f"  [{m.group(1).replace(' is expanded', '')}]")
    elif re.match(r'\s*uid=\S+ button "', l):
        m = re.search(r'button "([^"]+)"', l)
        if m:
            dish = m.group(1)
            skip = ['likes', 'like', 'comments', 'comment', 'Print', 'Get link', 'Add ', 'Dismiss',
                    'MORE INFO', 'EDIT', 'Open', 'TRY', 'collapse', 'Navigation', 'Go/Brew',
                    'Cha Chaan', 'Da Pai', 'CANCEL', 'Select']
            if not any(dish.startswith(s) for s in skip) and len(dish) > 2 and not dish[0].isdigit():
                print(f"    - {dish}")
PYEOF
```

### 5. 输出格式

用中文总结菜单，按餐厅分组，每个 station 列出主要菜品。翻译英文菜名为中文（如 Sweet & Sour Chicken → 咕噜鸡），Diet 标签可省略。最后给出推荐。

示例输出：
```
Da Pai Dang（大排档）— 25楼
- Hot Station: 白饭、意粉
- Protein: 烤肉眼牛排、咕噜鸡、虾仁炒蛋
- Vegetarian: 麻婆豆腐、Aloo Gobi
- Dessert: 黑巧克力蛋糕、西瓜

推荐：25楼大排档，烤肉眼 + 麻婆豆腐
```

## 页面结构说明

- **餐厅名** = heading level 3
- **Station 名** = heading level 4（如 Noodle Station, Hot Station）
- **菜品名** = button 元素（紧跟在 station heading 后面）
- **过敏原/配料** = heading level 6 "Contains:" / "Ingredients:" 下的 StaticText
- **likes/comments** = button "N likes" / "N comments"（忽略）
- **餐厅状态** = region "OPEN" / "CLOSED"，CLOSED 的会显示开门时间

## 注意事项

- 页面可能 loading 慢（显示 progressbar "Loading cafes"），等加载完再 snapshot
- location 默认跟随用户 desk location，如果不对可以点 location settings 切换
- Go/Brew 通常只提供咖啡饮品，不是正餐
- 如果用户没指定餐次，根据当前时间判断（<10:30 早餐，10:30-14:00 午餐，>14:00 下午茶）
