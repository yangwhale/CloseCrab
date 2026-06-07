---
name: live-canvas
description: Real-time SVG whiteboard for teaching. Draw shapes, arrows, text step-by-step while explaining concepts. Viewers watch on any browser. Use when giving visual explanations, drawing architecture diagrams, or teaching algorithms.
---

# Live Canvas — 实时教学白板

边画边讲的实时白板。Jarvis 通过 API 往 SVG 画布上逐步添加元素（矩形、箭头、文字、椭圆），所有连接的浏览器实时看到更新。用于给用户讲解技术概念时配合可视化。

## 触发场景

- 用户让你**画图讲解**某个概念（Flash Attention、MoE、并行策略等）
- 用户说**白板**、**画个图**、**可视化解释**
- 需要**架构图**配合语音讲解

## 架构

```
Jarvis (curl API)  ──→  cc-api.py (port 8766)  ──→  WebSocket  ──→  浏览器
                         ↓
                   HTTP API + SVG state
                         ↓
                   CC Pages (静态快照 fallback)
```

## 快速开始

### 1. 确保 live-board 在跑

```bash
# 检查
ss -tlnp | grep 8766

# 没跑的话启动
cd ~/CloseCrab && setsid python3 tools/cc-api.py --port 8766 > /tmp/live-board.log 2>&1 &
```

### 2. 画图 API

所有 API 都是 `POST http://localhost:8766/canvas/api/draw`，Content-Type: application/json。

#### 基础图元

```bash
API="http://localhost:8766/canvas/api"

# 设标题
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"title","title":"主题","subtitle":"副标题"}'

# 矩形 (必填: cmd, id, x, y, w, h)
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"rect","id":"r1","x":100,"y":100,"w":200,"h":80,
       "fill":"#e8f0fe","stroke":"#1a73e8","label":"标签",
       "fontSize":16,"labelColor":"#1a73e8","fontWeight":"600"}'

# 文字 (必填: cmd, id, x, y, text)
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"text","id":"t1","x":100,"y":50,"text":"Hello\n多行文字",
       "fontSize":14,"color":"#202124","anchor":"middle","fontWeight":"500"}'

# 箭头 (必填: cmd, id, x1, y1, x2, y2)
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"arrow","id":"a1","x1":200,"y1":180,"x2":200,"y2":300,
       "color":"#1a73e8","label":"数据流","fontSize":12}'

# 直线 (无箭头)
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"line","id":"l1","x1":0,"y1":100,"x2":960,"y2":100,
       "color":"#dadce0","dash":"4 4"}'

# 椭圆
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"ellipse","id":"e1","cx":480,"cy":270,"rx":60,"ry":40,
       "fill":"#fce4ec","stroke":"#c62828","label":"节点"}'

# SVG path (自由路径)
curl -s -X POST "$API/draw" -H 'Content-Type: application/json' \
  -d '{"cmd":"path","id":"p1","d":"M 100 100 Q 200 50 300 100",
       "stroke":"#1a73e8","fill":"none","markerEnd":"arrowhead-blue"}'
```

#### 批量操作

```bash
# 一次画多个元素
curl -s -X POST "$API/batch" -H 'Content-Type: application/json' \
  -d '{"commands":[
    {"cmd":"rect","id":"a","x":10,"y":10,"w":100,"h":50,"fill":"#bbdefb","stroke":"#1565c0","label":"A"},
    {"cmd":"rect","id":"b","x":200,"y":10,"w":100,"h":50,"fill":"#c8e6c9","stroke":"#2e7d32","label":"B"},
    {"cmd":"arrow","id":"ab","x1":110,"y1":35,"x2":198,"y2":35,"color":"#5f6368"}
  ]}'
```

#### 控制操作

```bash
# 高亮某元素 (闪烁动画)
curl -s -X POST "$API/draw" -d '{"cmd":"highlight","id":"r1","color":"#d32f2f"}'

# 删除某元素
curl -s -X POST "$API/draw" -d '{"cmd":"remove","id":"r1"}'

# 清空画布
curl -s -X POST "$API/draw" -d '{"cmd":"clear"}'

# 查看状态
curl -s "$API/state"
```

### 3. 发布静态快照到 CC Pages

当需要给用户一个可分享链接时，用 `scripts/export-canvas.py`：

```bash
python3 ~/CloseCrab/skills/live-canvas/scripts/export-canvas.py
# 输出: https://cc.higcp.com/pages/canvas-{topic}-{timestamp}.html
```

### 4. 用户观看

- **实时 WebSocket**: `http://localhost:8766/canvas/` (内网 / VPN)
- **CC Pages 静态快照**: `https://cc.higcp.com/pages/canvas-xxx.html` (外网, IAP)

## 画布坐标系

- ViewBox: `0 0 960 540` (16:9)
- 左上角 (0,0), 右下角 (960,540)
- 建议边距: 左右 60px, 上下 50px
- 可用区域: x=60~900, y=50~490

## 配色参考 (Material Design)

| 用途 | fill | stroke | labelColor |
|------|------|--------|------------|
| 蓝色强调 | #e8f0fe | #1a73e8 | #1a73e8 |
| 蓝色数据块 | #bbdefb | #1565c0 | #1565c0 |
| 绿色 | #e8f5e9 / #c8e6c9 | #2e7d32 | #2e7d32 |
| 红色/警告 | #fce4ec | #c62828 | #c62828 |
| 橙色 | #fff3e0 / #ffe0b2 | #e65100 | #e65100 |
| 紫色 | #f3e5f5 | #7b1fa2 | #7b1fa2 |
| 灰色/辅助 | #f5f5f5 | #9e9e9e | #5f6368 |
| 网格线 | — | #bdbdbd | — |

## 箭头颜色

预定义 marker: `arrowhead`(灰), `arrowhead-blue`, `arrowhead-red`, `arrowhead-green`。
arrow 的 `color` 字段自动匹配对应 marker。

## 讲课模式最佳实践

1. **先 clear 再画** — 每次新话题先 `{"cmd":"clear"}`
2. **设标题** — `{"cmd":"title","title":"Flash Attention","subtitle":"核心原理"}`
3. **逐步添加** — 每个 API 调用之间 `sleep 0.5~2`，配合语音节奏
4. **从大到小** — 先画大框架（矩形），再加细节（文字、箭头）
5. **highlight 引导视线** — 讲到某元素时 highlight 它
6. **batch 画相关元素** — 同时出现的元素用 batch 一起画
7. **语音模式配合** — voice channel 里边讲边画，每画一步停下来讲清楚

## 文件清单

| 文件 | 用途 |
|------|------|
| `tools/cc-api.py` | 服务端 (aiohttp + WebSocket) |
| `tools/board-canvas.html` | Canvas 前端模板 |
| `tools/board-page.html` | Slide 前端模板 (幻灯片模式) |
| `skills/live-canvas/scripts/export-canvas.py` | 导出静态快照到 CC Pages |
