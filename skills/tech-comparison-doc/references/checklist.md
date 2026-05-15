# Pre-Publish Checklist — 30 项发布前检查

技术对比文档发布前必须过一遍。按 6 大类组织，每项注明检查方法。

---

## 🏗️ A. HTML 结构完整性（5 项）

### A1. 标签配对验证 ⭐ P0
**检查**: 主要标签 open/close 数量必须相等
```bash
FILE=index.html
for tag in section div table tr td th pre code; do
  open=$(grep -oE "<$tag[ >]" $FILE | wc -l)
  close=$(grep -oE "</$tag>" $FILE | wc -l)
  status=$([ $open -eq $close ] && echo '✓' || echo '✗ MISMATCH')
  echo "$tag: open=$open close=$close $status"
done
```
**通过标准**: 所有标签 open == close

### A2. 自闭合标签格式 ⭐ P1
**检查**: `<br>`、`<img>`、`<input>` 等不需要 `</br>`，但 SVG 内 `<rect/>` `<line/>` 需要带斜杠
```bash
grep -nE '<(rect|line|circle|path|use|stop)[^/]*[^/]>$' $FILE | head -20
```
**通过标准**: SVG 元素都有自闭合斜杠（避免 IE/老浏览器渲染问题）

### A3. 内容截断扫描 ⭐ P0
**检查**: insight body / paragraph 中是否有半句话被截断
```bash
# 查找以 "是"、"了"、"和" 等连接词突然结尾的段落
grep -nE '(是|了|和|或|但|因为|所以)\s*$' $FILE | head -10
# 查找连续 2 行都以 < 开头但中间没有 > 的（可能截断）
awk 'BEGIN{prev=""} /^[[:space:]]*<[^>]*$/{if(prev~/^[[:space:]]*<[^>]*$/)print NR": potential broken tag"; prev=$0; next} {prev=$0}' $FILE
```
**通过标准**: 没有未完成的句子，没有断裂的标签

### A4. 字符编码 ⭐ P1
**检查**: HTML head 必须有 `<meta charset="utf-8">`
```bash
head -20 $FILE | grep -i 'charset'
```
**通过标准**: 有且只有一个 utf-8 声明

### A5. 死链检查 ⭐ P1
**检查**: 所有 `href`/`src` 链接是否可达
```bash
grep -oE 'href="https?://[^"]+"' $FILE | sort -u
# 手动 / 用 linkchecker 工具检查
```
**通过标准**: 外链全部可访问，内链锚点存在

---

## 🎨 B. SVG 质量（5 项）

### B1. viewBox 自适应 ⭐ P0
**检查**: 所有 SVG 必须有 viewBox + 自适应 style
```bash
grep -nE '<svg[^>]*>' $FILE | grep -v 'viewBox' && echo "✗ Missing viewBox"
grep -nE '<svg[^>]*>' $FILE | grep -v 'width:100%' && echo "✗ Missing responsive width"
```
**通过标准**: 所有 SVG 同时有 `viewBox` 和 `style="width:100%;max-width:Xpx"`

### B2. 文字密度 ⭐ P1
**检查**: 单元素文字 ≤ 2 行
**手动检查**: 浏览器打开页面缩放到 50%，看任何 SVG 框内文字是否拥挤
**通过标准**: 框高 ≥ 38px，文字间距 ≥ 12px，距框边 ≥ 6px

### B3. 三种视觉语言独立 ⭐ P1
**检查**: 单个 SVG 元素不要叠加多种视觉编码
- ❌ 反例：红色 + 粗线 + 渐变背景叠在一个 rect
- ✅ 正例：颜色编码归属、边框编码语义、背景编码区域
**手动检查**: 列出所有 rect 的 fill/stroke/stroke-width/stroke-dasharray 组合

### B4. 渐变禁令（Material Design 模式）⭐ P1
**检查**: page-style 是 Material Design 时禁用渐变
```bash
grep -nE '(linearGradient|radialGradient|background-clip:.*text)' $FILE
```
**通过标准**: 0 处使用（除非明确切换为 glassmorphism 风格）

### B5. 图例完整性 ⭐ P2
**检查**: SVG 用了 ≥ 4 种视觉编码必须有图例
**手动检查**: 每个有图例的 SVG，图例项数 ≥ 实际编码种类数
**通过标准**: 读者不需要看代码就能理解所有视觉符号

---

## 📊 C. 内容准确性（5 项）

### C1. 数据来源标注 ⭐ P0
**检查**: 所有性能数字、统计数据必须标来源
```bash
grep -cE 'perf-source|来源|数据来自|source:' $FILE
```
**通过标准**: 每个数据卡片 / 表格都有 `<div class="perf-source">` 或注释

### C2. 时间标注 ⭐ P0
**检查**: 数据必须标采集日期
```bash
grep -nE '(2026|2025).*(月|Q[1-4]|年|H[12]|-[0-9]{2}-)' $FILE | wc -l
```
**通过标准**: Hero 有日期、数据表有时间、Roadmap 有 ETA、Footer 有截止日期

### C3. 单位一致性 ⭐ P1
**检查**: 同类指标使用相同单位
- TFLOPs vs TFLOPS（统一一种）
- GB/s vs GB·s⁻¹（统一一种）
- µs vs us vs μs（统一 µs）
**手动检查**: 通读所有数据表

### C4. 版本号标注 ⭐ P1
**检查**: 框架版本 / 模型版本必须明确
**通过标准**: 涉及版本敏感数据时（如 "Llama 3.1-70B" 不是 "Llama 70B"）

### C5. 数据矛盾扫描 ⭐ P0
**检查**: 决策树结论 / 表格数据 / 文字描述之间不能矛盾
**手动检查**: 列出所有 "X 优于 Y" 类陈述，对照模型表 / 性能数据
**典型反例**: 决策树说 "400B+ 选 A"，但表格里 B 也支持 400B+

---

## ⚖️ D. 中立性（5 项）

### D1. 决策树不偏向单方 ⭐ P0
**检查**: 决策树终点必须有 "两者皆可" 兜底选项
**手动检查**: 看决策树，统计 A/B 终点数量，差距 ≤ 1

### D2. 总结段不写绝对判断 ⭐ P1
**检查**: 不出现 "X 是标准答案" / "Y 完胜" / "唯一选择"
```bash
grep -nE '(标准答案|完胜|唯一选择|the answer is|definitively)' $FILE
```
**通过标准**: 0 处出现（除非有数据支撑且明确限定场景）

### D3. "未列出" 语义明确 ⭐ P1
**检查**: 表格里 ❓ 必须区分 "作者没查" vs "对端不支持"
**通过标准**: caption 或 legend 明确说 "❓ 表示公开信息中未找到，不代表绝对不支持"

### D4. 营销语避免 ⭐ P2
**检查**: 不出现"颠覆性"、"革命性"、"黑科技"、"碾压"等
```bash
grep -nE '(颠覆|革命|黑科技|碾压|秒杀|吊打|王炸)' $FILE
```
**通过标准**: 0 处出现

### D5. 双方信息对称 ⭐ P2
**检查**: 两个对比对象的信息字段数应大致相同
**手动检查**: 双方 info-grid 字段数差距 ≤ 1，roadmap 项数差距 ≤ 2

---

## 📱 E. 响应式（5 项）

### E1. 移动端断点 ⭐ P1
**检查**: 必须有 `@media (max-width: 768px)` 处理
```bash
grep -cE '@media' $FILE
```
**通过标准**: 至少 1 个 media query（推荐 768px / 1024px 两个断点）

### E2. 大表格横向滚动 ⭐ P1
**检查**: 表格宽度 > 屏幕时必须有横向滚动容器
**手动检查**: 找到所有 `<table>`，外面应该包 `.tbl-wrap { overflow-x: auto }`

### E3. SVG 移动端可读 ⭐ P1
**检查**: 移动端 SVG 字号 ≥ 8px 等效（按 viewBox 比例计算）
**手动检查**: 浏览器开发工具切到 iPhone 13 (390x844)，所有 SVG 文字可读

### E4. 触控目标尺寸 ⭐ P2
**检查**: 链接 / 按钮触控目标 ≥ 44x44 px（iOS HIG）
**手动检查**: 移动端模式下点击所有交互元素

### E5. viewport meta ⭐ P0
**检查**: head 必须有 viewport meta
```bash
head -30 $FILE | grep -i 'name="viewport"'
```
**通过标准**: `<meta name="viewport" content="width=device-width, initial-scale=1">`

---

## 🚀 F. 发布流程（5 项）

### F1. CC Pages 上传方式 ⭐ P0
**检查**: 用 `gsutil cp` 不要依赖 gcsfuse 自动同步
```bash
gsutil cp index.html gs://chris-pgp-host-asia/cc-pages/pages/{topic}-$(date +%Y%m%d).html
```
**通过标准**: 上传后立即用 `curl -I` 验证 200 状态

### F2. URL 格式 ⭐ P1
**检查**: 链接不带引号、不带尾随空格
```
✅ https://cc.higcp.com/pages/topic-20260515.html
❌ "https://cc.higcp.com/pages/topic-20260515.html"  # 飞书会把引号当 URL 一部分
```

### F3. OG meta 标签 ⭐ P2
**检查**: head 必须有 og:title / og:description / og:image
```bash
grep -cE 'property="og:' $FILE
```
**通过标准**: ≥ 3 个 og 标签

### F4. OG 截图准备 ⭐ P2
**检查**: 提前生成 1200x630 截图作为 og:image
```bash
# 用 Playwright 或 Chrome MCP 截图
playwright screenshot --viewport-size 1200,630 \
  https://cc.higcp.com/pages/topic.html \
  $CC_PAGES_WEB_ROOT/assets/og-{topic}.png
```

### F5. Wiki 录入 ⭐ P2
**检查**: 长篇技术对比文档建议同步录入个人 Wiki
**通过标准**: 跑 wiki ingest，确认能被 wiki_query 命中

---

## 📋 快速 Pre-Flight 检查（≤ 2 分钟）

最低限度跑这 8 项 P0：
1. ✓ HTML 标签配对（A1）
2. ✓ 内容截断扫描（A3）
3. ✓ SVG viewBox 自适应（B1）
4. ✓ 数据来源标注（C1）
5. ✓ 时间标注（C2）
6. ✓ 数据矛盾扫描（C5）
7. ✓ 决策树不偏向（D1）
8. ✓ viewport meta（E5）

---

## 💡 检查脚本（一键跑 P0）

```bash
#!/bin/bash
# pre-publish-check.sh — 跑所有 P0 检查
FILE=${1:-index.html}
PASS=0
FAIL=0

check() {
  local name=$1
  local cmd=$2
  local result=$(eval "$cmd")
  if [ "$result" = "PASS" ]; then
    echo "✓ $name"
    PASS=$((PASS+1))
  else
    echo "✗ $name: $result"
    FAIL=$((FAIL+1))
  fi
}

# A1: 标签配对
for tag in section div table; do
  o=$(grep -oE "<$tag[ >]" $FILE | wc -l)
  c=$(grep -oE "</$tag>" $FILE | wc -l)
  check "A1.$tag pairing" "[ $o -eq $c ] && echo PASS || echo 'open=$o close=$c'"
done

# A3: 截断扫描
truncated=$(grep -cE '(是|了|和|或|但)\s*$' $FILE)
check "A3 truncation scan" "[ $truncated -eq 0 ] && echo PASS || echo '$truncated suspicious lines'"

# B1: SVG viewBox
no_vb=$(grep -nE '<svg[^>]*>' $FILE | grep -vc 'viewBox')
check "B1 SVG viewBox" "[ $no_vb -eq 0 ] && echo PASS || echo '$no_vb SVGs missing viewBox'"

# C1: 数据来源
sources=$(grep -cE 'perf-source|来源:|source:' $FILE)
check "C1 data sources" "[ $sources -gt 0 ] && echo PASS || echo 'no data sources marked'"

# E5: viewport meta
vp=$(head -30 $FILE | grep -c 'name="viewport"')
check "E5 viewport meta" "[ $vp -eq 1 ] && echo PASS || echo '$vp viewport metas (expect 1)'"

echo ""
echo "Result: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && exit 0 || exit 1
```

把这个脚本存到 `references/pre-publish-check.sh`，发布前跑一遍。
