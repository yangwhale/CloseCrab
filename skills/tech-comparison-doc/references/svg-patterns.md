# SVG Patterns — 4 个完整可运行的 SVG 模板

技术对比文档常用的 4 种 SVG 图表模板，全部使用 **Material Design 配色**（无渐变、无毛玻璃）。复制即可使用。

## 配色规范（Material Design）

```
Primary:    #1A73E8  (Google Blue)
Secondary:  #0D9488  (Teal)
Success:    #137333  (Google Green)
Warning:    #F9AB00  (Google Yellow)
Danger:     #D93025  (Google Red)
Text:       #202124  (主标题)
Text-2:     #5F6368  (次要文字)
Text-3:     #9AA0A6  (辅助文字)
Border:     #DADCE0  (1px 边框)
Surface:    #FFFFFF  (卡片背景)
Bg:         #F8F9FA  (页面背景)
```

---

## Pattern 1: Architecture Diagram（主架构图）

**用途**: Section 04 静态组件结构展示
**特点**: 展示 "三种视觉语言原则"
- 颜色编码 = 主体归属（蓝 Primary / 青 Secondary / 灰 共享）
- 边框样式 = 语义类型（dashed=插件边界 / solid=普通 / 粗线=独有优势）
- 背景填充 = 区域分组

```html
<svg viewBox="0 0 880 480" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:880px;display:block;margin:0 auto;
            font-family:'Google Sans','Roboto',Inter,sans-serif">
  <defs>
    <marker id="arr-p" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#1A73E8" stroke-width="1.2"/>
    </marker>
    <marker id="arr-s" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#0D9488" stroke-width="1.2"/>
    </marker>
    <marker id="arr-g" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#5F6368" stroke-width="1.2"/>
    </marker>
  </defs>

  <!-- ================ 区域分组：背景填充 ================ -->
  <!-- Framework A 区域（极浅蓝） -->
  <rect x="20" y="40" width="400" height="400" rx="12"
        fill="#E8F0FE" fill-opacity="0.4" stroke="#1A73E8" stroke-opacity="0.15"/>
  <text x="40" y="64" fill="#1A73E8" font-size="11" font-weight="600"
        letter-spacing="0.04em">FRAMEWORK A</text>

  <!-- Framework B 区域（极浅青） -->
  <rect x="460" y="40" width="400" height="400" rx="12"
        fill="#E0F2F1" fill-opacity="0.4" stroke="#0D9488" stroke-opacity="0.15"/>
  <text x="480" y="64" fill="#0D9488" font-size="11" font-weight="600"
        letter-spacing="0.04em">FRAMEWORK B</text>

  <!-- ================ 顶层：用户接口（普通框） ================ -->
  <rect x="40" y="84" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-width="1"/>
  <text x="220" y="106" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">HTTP / OpenAI API</text>
  <text x="220" y="122" text-anchor="middle" fill="#5F6368" font-size="10">
    标准接口层
  </text>

  <rect x="480" y="84" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-width="1"/>
  <text x="660" y="106" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">HTTP / Custom API</text>
  <text x="660" y="122" text-anchor="middle" fill="#5F6368" font-size="10">
    原生接口层
  </text>

  <!-- ================ 中层：调度（粗线 = 独有优势） ================ -->
  <rect x="40" y="156" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-width="2.5"/>
  <text x="220" y="178" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="600">Scheduler + 高级特性</text>
  <text x="220" y="194" text-anchor="middle" fill="#1A73E8" font-size="9.5"
        font-weight="600">★ 独有优势（粗线标记）</text>

  <rect x="480" y="156" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-width="1"/>
  <text x="660" y="178" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">Standard Scheduler</text>
  <text x="660" y="194" text-anchor="middle" fill="#5F6368" font-size="10">
    通用调度
  </text>

  <!-- ================ 中下层：插件边界（dashed） ================ -->
  <rect x="40" y="228" width="360" height="48" rx="6"
        fill="#F8F9FA" stroke="#1A73E8" stroke-width="1.5"
        stroke-dasharray="5 3"/>
  <text x="220" y="250" text-anchor="middle" fill="#1A73E8"
        font-size="12" font-weight="600">Plugin Layer</text>
  <text x="220" y="266" text-anchor="middle" fill="#5F6368" font-size="9.5">
    插件边界（dashed 标记）
  </text>

  <rect x="480" y="228" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-width="1"/>
  <text x="660" y="250" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">Native Engine</text>
  <text x="660" y="266" text-anchor="middle" fill="#5F6368" font-size="10">
    无插件层，直连
  </text>

  <!-- ================ 底层：共享基础（灰色 = 共享） ================ -->
  <rect x="40" y="300" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#5F6368" stroke-width="1"/>
  <text x="220" y="322" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">XLA Compiler</text>
  <text x="220" y="338" text-anchor="middle" fill="#5F6368" font-size="10">
    共享编译器
  </text>

  <rect x="480" y="300" width="360" height="48" rx="6"
        fill="#FFFFFF" stroke="#5F6368" stroke-width="1"/>
  <text x="660" y="322" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="500">XLA Compiler</text>
  <text x="660" y="338" text-anchor="middle" fill="#5F6368" font-size="10">
    共享编译器
  </text>

  <!-- 共享硬件层（横跨两侧） -->
  <rect x="40" y="372" width="800" height="48" rx="6"
        fill="#F1F3F4" stroke="#5F6368" stroke-width="1"/>
  <text x="440" y="394" text-anchor="middle" fill="#202124"
        font-size="13" font-weight="600">TPU Hardware</text>
  <text x="440" y="410" text-anchor="middle" fill="#5F6368" font-size="10">
    共享硬件目标
  </text>

  <!-- ================ 箭头连接 ================ -->
  <line x1="220" y1="132" x2="220" y2="154" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-p)"/>
  <line x1="220" y1="204" x2="220" y2="226" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-p)"/>
  <line x1="220" y1="276" x2="220" y2="298" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-p)"/>
  <line x1="220" y1="348" x2="220" y2="370" stroke="#5F6368"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-g)"/>

  <line x1="660" y1="132" x2="660" y2="154" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-s)"/>
  <line x1="660" y1="204" x2="660" y2="226" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-s)"/>
  <line x1="660" y1="276" x2="660" y2="298" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-s)"/>
  <line x1="660" y1="348" x2="660" y2="370" stroke="#5F6368"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#arr-g)"/>

  <!-- ================ 图例 ================ -->
  <rect x="20" y="450" width="14" height="14" rx="2"
        fill="#FFFFFF" stroke="#1A73E8" stroke-width="2.5"/>
  <text x="40" y="461" fill="#5F6368" font-size="10">独有优势（粗线）</text>

  <rect x="180" y="450" width="14" height="14" rx="2"
        fill="#F8F9FA" stroke="#1A73E8" stroke-width="1.5"
        stroke-dasharray="3 2"/>
  <text x="200" y="461" fill="#5F6368" font-size="10">插件边界（dashed）</text>

  <rect x="340" y="450" width="14" height="14" rx="2"
        fill="#F1F3F4" stroke="#5F6368"/>
  <text x="360" y="461" fill="#5F6368" font-size="10">共享组件（灰色）</text>
</svg>
```

**关键点**:
- viewBox `0 0 880 480` 配 `width:100%;max-width:880px` 自适应
- 三种视觉语言**独立运作**（不要把"红色 + 粗线 + 渐变背景"叠在一个元素上）
- 单元素文字 ≤ 2 行，距框边 ≥ 6px
- 共享硬件层横跨两侧，传达"目标硬件相同"

---

## Pattern 2: Swimlane（数据流泳道）

**用途**: Section 05 动态请求生命周期对比
**特点**: 双泳道 × N 步 × 共享区背景 + 跨泳道连接器

```html
<svg viewBox="0 0 960 290" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:960px;display:block;margin:0 auto;
            font-family:'Google Sans','Roboto',Inter,sans-serif">
  <defs>
    <marker id="sw-a" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
      <path d="M0,0.5 L5,2.5 L0,4.5" fill="none" stroke="#1A73E8" stroke-width="1.2"/>
    </marker>
    <marker id="sw-b" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
      <path d="M0,0.5 L5,2.5 L0,4.5" fill="none" stroke="#0D9488" stroke-width="1.2"/>
    </marker>
    <marker id="sw-g" markerWidth="6" markerHeight="5" refX="5" refY="2.5" orient="auto">
      <path d="M0,0.5 L5,2.5 L0,4.5" fill="none" stroke="#137333" stroke-width="1.2"/>
    </marker>
  </defs>

  <!-- 共享区背景（绿色极浅） -->
  <rect x="450" y="24" width="268" height="234" rx="8"
        fill="#E6F4EA" fill-opacity="0.4" stroke="#137333"
        stroke-opacity="0.2" stroke-width="1" stroke-dasharray="4 3"/>
  <text x="584" y="20" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="700" letter-spacing="0.06em">共享组件区</text>

  <!-- Lane A 背景 -->
  <rect x="8" y="26" width="942" height="108" rx="10"
        fill="#E8F0FE" fill-opacity="0.25" stroke="#1A73E8"
        stroke-opacity="0.15" stroke-width="1"/>
  <text x="14" y="22" fill="#1A73E8" font-size="9" font-weight="700"
        letter-spacing="0.05em">FRAMEWORK A</text>

  <!-- Lane B 背景 -->
  <rect x="8" y="148" width="942" height="108" rx="10"
        fill="#E0F2F1" fill-opacity="0.25" stroke="#0D9488"
        stroke-opacity="0.15" stroke-width="1"/>
  <text x="14" y="144" fill="#0D9488" font-size="9" font-weight="700"
        letter-spacing="0.05em">FRAMEWORK B</text>

  <!-- ============ Lane A 步骤 (蓝, y=50, 8 步) ============ -->
  <!-- 复用规则：每框 78x38, 间隔 10, 起点 x=15 -->
  <!-- x = 15, 103, 191, 279, 367, 455, 543, 631, 719, 807 -->

  <rect x="15" y="50" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-opacity="0.3"/>
  <text x="54" y="68" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">HTTP</text>
  <text x="54" y="80" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">Request</text>

  <rect x="103" y="50" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-opacity="0.3"/>
  <text x="142" y="68" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">Tokenize</text>

  <rect x="191" y="50" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-opacity="0.3"/>
  <text x="230" y="68" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">Schedule</text>

  <!-- 插件边界（dashed） -->
  <rect x="279" y="50" width="78" height="38" rx="6"
        fill="#E8F0FE" stroke="#1A73E8" stroke-opacity="0.55"
        stroke-width="1.5" stroke-dasharray="4 2"/>
  <text x="318" y="65" text-anchor="middle" fill="#1A73E8"
        font-size="7.5" font-weight="600">Plugin</text>
  <text x="318" y="76" text-anchor="middle" fill="#1A73E8"
        font-size="7" fill-opacity="0.7">边界</text>

  <rect x="367" y="50" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-opacity="0.3"/>
  <text x="406" y="68" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">Adapter</text>

  <!-- 共享步骤 1 (绿色) -->
  <rect x="455" y="50" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="494" y="68" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Compile</text>

  <!-- 共享步骤 2 -->
  <rect x="543" y="50" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="582" y="68" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Execute</text>

  <!-- 共享步骤 3 -->
  <rect x="631" y="50" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="670" y="68" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Detokenize</text>

  <rect x="719" y="50" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#1A73E8" stroke-opacity="0.3"/>
  <text x="758" y="68" text-anchor="middle" fill="#1A73E8"
        font-size="8" font-weight="600">Response</text>

  <!-- Lane A 箭头 -->
  <line x1="93" y1="69" x2="101" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>
  <line x1="181" y1="69" x2="189" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>
  <line x1="269" y1="69" x2="277" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>
  <line x1="357" y1="69" x2="365" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>
  <line x1="445" y1="69" x2="453" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>
  <line x1="533" y1="69" x2="541" y2="69" stroke="#137333"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-g)"/>
  <line x1="621" y1="69" x2="629" y2="69" stroke="#137333"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-g)"/>
  <line x1="709" y1="69" x2="717" y2="69" stroke="#1A73E8"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-a)"/>

  <!-- ============ Lane B 步骤 (青, y=170, 9 步) ============ -->
  <rect x="15" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.3"/>
  <text x="54" y="188" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">HTTP</text>
  <text x="54" y="200" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">Request</text>

  <rect x="103" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.3"/>
  <text x="142" y="188" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">Tokenize</text>

  <!-- 独有优势（粗线 + 标签） -->
  <rect x="191" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.6"
        stroke-width="2"/>
  <text x="230" y="184" text-anchor="middle" fill="#0D9488"
        font-size="7.5" font-weight="600">Advanced</text>
  <text x="230" y="194" text-anchor="middle" fill="#0D9488"
        font-size="7.5" font-weight="700">Scheduler</text>
  <text x="230" y="204" text-anchor="middle" fill="#0D9488"
        font-size="6.5" fill-opacity="0.7">★ 独有</text>

  <rect x="279" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.3"/>
  <text x="318" y="188" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">Worker</text>

  <rect x="367" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.3"/>
  <text x="406" y="188" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">Model</text>

  <!-- 共享步骤（同 Lane A 的绿色框） -->
  <rect x="455" y="170" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="494" y="188" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Compile</text>

  <rect x="543" y="170" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="582" y="188" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Execute</text>

  <rect x="631" y="170" width="78" height="38" rx="6"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.4"/>
  <text x="670" y="188" text-anchor="middle" fill="#137333"
        font-size="8" font-weight="600">Detokenize</text>

  <rect x="719" y="170" width="78" height="38" rx="6"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.3"/>
  <text x="758" y="188" text-anchor="middle" fill="#0D9488"
        font-size="8" font-weight="600">Response</text>

  <!-- Lane B 箭头 -->
  <line x1="93" y1="189" x2="101" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>
  <line x1="181" y1="189" x2="189" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>
  <line x1="269" y1="189" x2="277" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>
  <line x1="357" y1="189" x2="365" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>
  <line x1="445" y1="189" x2="453" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>
  <line x1="533" y1="189" x2="541" y2="189" stroke="#137333"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-g)"/>
  <line x1="621" y1="189" x2="629" y2="189" stroke="#137333"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-g)"/>
  <line x1="709" y1="189" x2="717" y2="189" stroke="#0D9488"
        stroke-opacity="0.5" stroke-width="1.2" marker-end="url(#sw-b)"/>

  <!-- 跨泳道连接器（共享步骤的虚线） -->
  <line x1="494" y1="88" x2="494" y2="170" stroke="#137333"
        stroke-opacity="0.3" stroke-width="1" stroke-dasharray="3 3"/>
  <line x1="582" y1="88" x2="582" y2="170" stroke="#137333"
        stroke-opacity="0.3" stroke-width="1" stroke-dasharray="3 3"/>
  <line x1="670" y1="88" x2="670" y2="170" stroke="#137333"
        stroke-opacity="0.3" stroke-width="1" stroke-dasharray="3 3"/>

  <!-- ============ 图例 ============ -->
  <rect x="12" y="266" width="9" height="9" rx="2"
        fill="#E6F4EA" stroke="#137333" stroke-opacity="0.5"/>
  <text x="24" y="274" fill="#5F6368" font-size="8">共享组件</text>

  <rect x="120" y="266" width="9" height="9" rx="2"
        fill="#E8F0FE" stroke="#1A73E8" stroke-opacity="0.5"
        stroke-width="1.5" stroke-dasharray="3 2"/>
  <text x="132" y="274" fill="#5F6368" font-size="8">插件边界（dashed）</text>

  <rect x="280" y="266" width="9" height="9" rx="2"
        fill="#FFFFFF" stroke="#0D9488" stroke-opacity="0.6"
        stroke-width="2"/>
  <text x="292" y="274" fill="#5F6368" font-size="8">独有优势（粗线）</text>

  <line x1="430" y1="270" x2="450" y2="270" stroke="#137333"
        stroke-opacity="0.4" stroke-width="1" stroke-dasharray="3 3"/>
  <text x="454" y="274" fill="#5F6368" font-size="8">跨泳道共享连接</text>
</svg>
```

**关键点**:
- 双泳道 y 间隔 ≥ 14px（lane1 底 y=134，lane2 顶 y=148）
- 共享步骤背景同色（绿色 #E6F4EA），传达 "走同一个底层"
- 跨泳道虚线连接共享步骤，强化"共享"语义
- 进入/离开共享区时箭头颜色切换（绿 ↔ 框架色）

---

## Pattern 3: Decision Tree（决策流程图）

**用途**: Section 09 选型决策流程
**特点**: 节点 ≤ 5 个，每个 Yes/No 分支必须明确

```html
<svg viewBox="0 0 880 410" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:880px;display:block;margin:0 auto;
            font-family:'Google Sans','Roboto',Inter,sans-serif">
  <defs>
    <marker id="dt-d" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#5F6368" stroke-width="1.2"/>
    </marker>
    <marker id="dt-a" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#1A73E8" stroke-width="1.2"/>
    </marker>
    <marker id="dt-b" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#0D9488" stroke-width="1.2"/>
    </marker>
  </defs>

  <!-- Start 节点（深色 pill） -->
  <rect x="340" y="10" width="200" height="40" rx="20"
        fill="#202124"/>
  <text x="440" y="35" text-anchor="middle" fill="#FFFFFF"
        font-size="13" font-weight="600">你的场景是什么？</text>
  <line x1="440" y1="50" x2="440" y2="75" stroke="#5F6368"
        stroke-width="1.2" marker-end="url(#dt-d)"/>

  <!-- Q1 -->
  <rect x="310" y="75" width="260" height="44" rx="8"
        fill="#FFFFFF" stroke="#DADCE0"/>
  <text x="440" y="97" text-anchor="middle" fill="#202124"
        font-size="11" font-weight="500">已经在用 Framework A 的相关生态？</text>
  <text x="440" y="112" text-anchor="middle" fill="#9AA0A6"
        font-size="9.5">想保持同一套 API / 运维</text>
  <text x="585" y="93" fill="#1A73E8" font-size="10" font-weight="600">Yes</text>
  <line x1="570" y1="97" x2="660" y2="97" stroke="#1A73E8"
        stroke-width="1.5" marker-end="url(#dt-a)"/>
  <rect x="660" y="77" width="180" height="40" rx="10"
        fill="#E8F0FE" stroke="#1A73E8" stroke-width="1.5"/>
  <text x="750" y="101" text-anchor="middle" fill="#1A73E8"
        font-size="12" font-weight="700">→ Framework A</text>
  <text x="425" y="138" fill="#5F6368" font-size="10" font-weight="500">No</text>
  <line x1="440" y1="119" x2="440" y2="150" stroke="#5F6368"
        stroke-width="1.2" marker-end="url(#dt-d)"/>

  <!-- Q2 -->
  <rect x="310" y="150" width="260" height="44" rx="8"
        fill="#FFFFFF" stroke="#DADCE0"/>
  <text x="440" y="172" text-anchor="middle" fill="#202124"
        font-size="11" font-weight="500">需要 Framework B 的独有特性？</text>
  <text x="440" y="187" text-anchor="middle" fill="#9AA0A6"
        font-size="9.5">如线性注意力 / 前沿模型快速跟进</text>
  <text x="585" y="168" fill="#0D9488" font-size="10" font-weight="600">Yes</text>
  <line x1="570" y1="172" x2="660" y2="172" stroke="#0D9488"
        stroke-width="1.5" marker-end="url(#dt-b)"/>
  <rect x="660" y="152" width="180" height="40" rx="10"
        fill="#E0F2F1" stroke="#0D9488" stroke-width="1.5"/>
  <text x="750" y="176" text-anchor="middle" fill="#0D9488"
        font-size="12" font-weight="700">→ Framework B</text>
  <text x="425" y="213" fill="#5F6368" font-size="10" font-weight="500">No</text>
  <line x1="440" y1="194" x2="440" y2="225" stroke="#5F6368"
        stroke-width="1.2" marker-end="url(#dt-d)"/>

  <!-- Q3：复合条件加 "且" -->
  <rect x="310" y="225" width="260" height="44" rx="8"
        fill="#FFFFFF" stroke="#DADCE0"/>
  <text x="440" y="247" text-anchor="middle" fill="#202124"
        font-size="11" font-weight="500">需求规模大且追求生产稳定？</text>
  <text x="440" y="262" text-anchor="middle" fill="#9AA0A6"
        font-size="9.5">如超大模型 / 企业级 SLA</text>
  <text x="585" y="243" fill="#1A73E8" font-size="10" font-weight="600">Yes</text>
  <line x1="570" y1="247" x2="660" y2="247" stroke="#1A73E8"
        stroke-width="1.5" marker-end="url(#dt-a)"/>
  <rect x="660" y="227" width="180" height="40" rx="10"
        fill="#E8F0FE" stroke="#1A73E8" stroke-width="1.5"/>
  <text x="750" y="251" text-anchor="middle" fill="#1A73E8"
        font-size="12" font-weight="700">→ Framework A</text>
  <text x="425" y="288" fill="#5F6368" font-size="10" font-weight="500">No</text>
  <line x1="440" y1="269" x2="440" y2="300" stroke="#5F6368"
        stroke-width="1.2" marker-end="url(#dt-d)"/>

  <!-- Q4 -->
  <rect x="310" y="300" width="260" height="44" rx="8"
        fill="#FFFFFF" stroke="#DADCE0"/>
  <text x="440" y="322" text-anchor="middle" fill="#202124"
        font-size="11" font-weight="500">需要 Framework B 的特定优化？</text>
  <text x="440" y="337" text-anchor="middle" fill="#9AA0A6"
        font-size="9.5">如 FP8 量化 / 推测解码 / 多模态</text>
  <text x="585" y="318" fill="#0D9488" font-size="10" font-weight="600">Yes</text>
  <line x1="570" y1="322" x2="660" y2="322" stroke="#0D9488"
        stroke-width="1.5" marker-end="url(#dt-b)"/>
  <rect x="660" y="302" width="180" height="40" rx="10"
        fill="#E0F2F1" stroke="#0D9488" stroke-width="1.5"/>
  <text x="750" y="326" text-anchor="middle" fill="#0D9488"
        font-size="12" font-weight="700">→ Framework B</text>
  <text x="425" y="363" fill="#5F6368" font-size="10" font-weight="500">No</text>
  <line x1="440" y1="344" x2="440" y2="368" stroke="#5F6368"
        stroke-width="1.2" marker-end="url(#dt-d)"/>

  <!-- 终点：两者皆可（绿色，避免读者被推向单一选项） -->
  <rect x="320" y="368" width="240" height="32" rx="16"
        fill="#E6F4EA" stroke="#137333" stroke-width="1.2"/>
  <text x="440" y="389" text-anchor="middle" fill="#137333"
        font-size="11" font-weight="600">✓ 两者皆可，按技术栈偏好选择</text>
</svg>
```

**关键点**:
- 节点 ≤ 5 个（4 个 Q + 1 个终点），更多读者会看不下去
- 复合条件用 "且" 而非 "或"（如 Q3 "规模大**且**追求生产稳定"）
- 终点必须给"两者皆可"的兜底选项，避免读者被推向单一选项
- Yes/No 分支颜色区分（Yes=框架色，No=灰色）

---

## Pattern 4: Flat Material（无渐变 SVG）

**用途**: Section 04 简化架构图、Section 03 概念示意图
**特点**: 完全无渐变、无阴影、纯几何 + Material Design 配色

```html
<svg viewBox="0 0 720 320" xmlns="http://www.w3.org/2000/svg"
     style="width:100%;max-width:720px;display:block;margin:0 auto;
            font-family:'Google Sans','Roboto',Inter,sans-serif">
  <defs>
    <marker id="fm-arr" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto">
      <path d="M0,0 L8,3 L0,6" fill="none" stroke="#5F6368" stroke-width="1.5"/>
    </marker>
  </defs>

  <!-- 背景：纯白 -->
  <rect x="0" y="0" width="720" height="320" fill="#FFFFFF"/>

  <!-- 三层架构 -->
  <!-- L1: API Layer -->
  <rect x="60" y="40" width="600" height="56" rx="4"
        fill="#FFFFFF" stroke="#DADCE0" stroke-width="1"/>
  <text x="80" y="64" fill="#202124" font-size="14" font-weight="600">
    API Layer
  </text>
  <text x="80" y="83" fill="#5F6368" font-size="11">
    OpenAI 兼容 · gRPC · WebSocket
  </text>

  <!-- L2: Engine Layer -->
  <rect x="60" y="120" width="290" height="100" rx="4"
        fill="#E8F0FE" stroke="#1A73E8" stroke-width="1"/>
  <text x="80" y="146" fill="#1A73E8" font-size="13" font-weight="600">
    Framework A Engine
  </text>
  <text x="80" y="166" fill="#202124" font-size="11">• Plugin-based</text>
  <text x="80" y="184" fill="#202124" font-size="11">• Generic scheduler</text>
  <text x="80" y="202" fill="#202124" font-size="11">• Multi-backend</text>

  <rect x="370" y="120" width="290" height="100" rx="4"
        fill="#E0F2F1" stroke="#0D9488" stroke-width="1"/>
  <text x="390" y="146" fill="#0D9488" font-size="13" font-weight="600">
    Framework B Engine
  </text>
  <text x="390" y="166" fill="#202124" font-size="11">• Native architecture</text>
  <text x="390" y="184" fill="#202124" font-size="11">• Specialized scheduler</text>
  <text x="390" y="202" fill="#202124" font-size="11">• Single-backend optimized</text>

  <!-- L3: Hardware Layer -->
  <rect x="60" y="244" width="600" height="56" rx="4"
        fill="#F1F3F4" stroke="#5F6368" stroke-width="1"/>
  <text x="80" y="268" fill="#202124" font-size="14" font-weight="600">
    Hardware Layer
  </text>
  <text x="80" y="287" fill="#5F6368" font-size="11">
    TPU · GPU · 通过 XLA / CUDA 编译
  </text>

  <!-- 连接箭头（纯灰色，无装饰） -->
  <line x1="205" y1="96" x2="205" y2="118" stroke="#5F6368"
        stroke-width="1.5" marker-end="url(#fm-arr)"/>
  <line x1="515" y1="96" x2="515" y2="118" stroke="#5F6368"
        stroke-width="1.5" marker-end="url(#fm-arr)"/>
  <line x1="205" y1="220" x2="205" y2="242" stroke="#5F6368"
        stroke-width="1.5" marker-end="url(#fm-arr)"/>
  <line x1="515" y1="220" x2="515" y2="242" stroke="#5F6368"
        stroke-width="1.5" marker-end="url(#fm-arr)"/>
</svg>
```

**关键点**:
- 完全无 `<linearGradient>` / `<filter>` / `feDropShadow`
- 纯白背景 + 1px 边框 + 极浅蓝/青区域填充（#E8F0FE / #E0F2F1）
- 文字层级：`#202124` 标题 / `#5F6368` 说明 / `#9AA0A6` 辅助
- 适合 page-style skill 的 Material Design 偏好

---

## 通用规则总结

1. **viewBox 优先于 width/height**：`viewBox="0 0 W H" style="width:100%;max-width:Wpx"` 自适应
2. **三种视觉语言独立运作**：颜色 + 边框样式 + 背景填充，不要叠加在一个元素上
3. **文字密度限制**：单元素 ≤ 2 行、距框边 ≥ 6px、字号 ≥ 7px（移动端 ≥ 8px）
4. **箭头颜色随区域切换**：进入共享区用绿，离开回到框架色
5. **图例必须有**：4 种以上视觉编码必须解释
6. **决策树兜底**：终点必须给 "两者皆可" 选项
7. **Material Design 默认无渐变**：除非明确切换风格，不用 `<linearGradient>`
