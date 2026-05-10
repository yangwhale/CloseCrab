# Slides JSON Schema

Each question needs a JSON file describing its slide content. `gen_slides.py` reads this JSON and produces an HTML slide deck.

## Required Fields

```json
{
  "q_num": 2,
  "topic": "盈亏问题",
  "subtitle": "差额法求人数，一步到位",
  "problem_text": "题目文字（简洁版，适合一屏显示）",
  "answer": "8031 元",
  "steps": [
    {
      "badge": "找差额",
      "title": "每人收费差多少？",
      "content_html": "<div class=\"math-block\">...</div>"
    }
  ],
  "summary_when": "什么时候用这个方法",
  "summary_how": "怎么用这个方法",
  "tip": "记忆口诀或注意事项"
}
```

## Optional Fields

| Field | Default | Description |
|-------|---------|-------------|
| `header` | `第{q_num}题` | Badge and title prefix |
| `exam_title` | `数学教学视频` | Top line on title slide |
| `grade_label` | `— 适合小学生 —` | Bottom line on title slide |
| `subtitle` | `""` | Second line on title slide |

## Steps Array

3-5 steps recommended. Each step becomes one slide.

- `badge` — short label in top-left corner (2-4 chars)
- `title` — step heading (under 15 chars)
- `content_html` — HTML fragment, supports these CSS classes:

## Content HTML Classes

### Math Blocks
```html
<div class="math-block">standard math expression</div>
<div class="math-block highlight">key result (green border)</div>
<div class="math-block cancel">wrong approach (red border)</div>
```

### Highlight Colors
```html
<span class="hl-blue">blue (constants, given values)</span>
<span class="hl-green">green (intermediate results)</span>
<span class="hl-amber">amber (key quantities)</span>
<span class="hl-rose">rose (answers, differences)</span>
<span class="hl-purple">purple (totals, sums)</span>
```

## Example

See the full example in `q2_slides.json` from the 华夏杯 production run:

```json
{
  "q_num": 2,
  "topic": "盈亏问题",
  "subtitle": "差额法求人数，一步到位",
  "exam_title": "华夏杯 2026 · 小五模拟试题",
  "grade_label": "— 适合五年级学生 —",
  "problem_text": "华太师请学生吃饭。每位收 201 元，差 2019 元；每位收 136 元，多 1231 元。预算多少？",
  "answer": "8031 元",
  "steps": [
    {
      "badge": "找差额",
      "title": "每人收费差多少？",
      "content_html": "<div class=\"math-block\"><span class=\"hl-blue\">201</span> − <span class=\"hl-green\">136</span> = <span class=\"hl-rose\">65</span> 元/人</div><div>每个人多收了 <span class=\"hl-rose\">65 元</span></div>"
    },
    {
      "badge": "总差额",
      "title": "一亏一盈，总共差多少？",
      "content_html": "<div class=\"math-block\"><span class=\"hl-blue\">2019</span>（亏）+ <span class=\"hl-green\">1231</span>（盈）= <span class=\"hl-purple\">3250</span> 元</div><div>从「不够」到「多出来」，总差 <span class=\"hl-purple\">3250 元</span></div>"
    },
    {
      "badge": "求人数",
      "title": "总差额 ÷ 每人差额 = 人数",
      "content_html": "<div class=\"math-block\"><span class=\"hl-purple\">3250</span> ÷ <span class=\"hl-rose\">65</span> = <span class=\"hl-amber\">50</span> 人</div>"
    },
    {
      "badge": "求预算",
      "title": "代回去算预算",
      "content_html": "<div class=\"math-block\">201 × <span class=\"hl-amber\">50</span> − 2019 = 10050 − 2019 = <span class=\"hl-rose\">8031</span> 元</div><div>验证：136 × 50 = 6800，8031 − 6800 = <span class=\"hl-green\">1231</span> ✓</div>"
    }
  ],
  "summary_when": "看到「多了 / 少了 / 盈 / 亏」的两种分配方案",
  "summary_how": "总差额 ÷ 单位差额 = 份数，再代入求总量",
  "tip": "一盈一亏加起来，同盈同亏要相减"
}
```
