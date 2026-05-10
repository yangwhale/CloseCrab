#!/usr/bin/env python3
"""Generate slide HTML from a JSON content file.

Usage: gen_slides.py <input.json> <output.html>

The JSON schema is documented in references/slides-schema.md.
"""
import json, sys

TEMPLATE = '''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@300;400;500;700;900&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: 'Noto Sans SC', sans-serif; background: #0f172a; color: #e2e8f0; }}
  .slide {{
    width: 1920px; height: 1080px;
    display: flex; flex-direction: column; justify-content: center; align-items: center;
    padding: 80px 120px; position: relative; overflow: hidden;
  }}
  .slide::before {{
    content: ''; position: absolute; top: -200px; right: -200px;
    width: 500px; height: 500px; border-radius: 50%;
    background: radial-gradient(circle, rgba(56,189,248,0.08) 0%, transparent 70%);
  }}
  .slide-title {{ background: linear-gradient(135deg, #1e3a5f 0%, #0f172a 50%, #1a1a2e 100%); }}
  .slide-problem {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); }}
  .slide-step {{ background: linear-gradient(160deg, #0f172a 0%, #162032 100%); }}
  .slide-answer {{ background: linear-gradient(135deg, #064e3b 0%, #0f172a 60%); }}
  .slide-summary {{ background: linear-gradient(135deg, #1e1b4b 0%, #0f172a 60%); }}
  .slide-number {{ position: absolute; top: 40px; right: 60px; font-size: 20px; color: #475569; font-weight: 300; letter-spacing: 2px; }}
  .badge {{ position: absolute; top: 40px; left: 60px; background: rgba(56,189,248,0.15); border: 1px solid rgba(56,189,248,0.3); color: #38bdf8; padding: 8px 24px; border-radius: 20px; font-size: 16px; font-weight: 500; }}
  .title-main {{ font-size: 72px; font-weight: 900; background: linear-gradient(135deg, #38bdf8, #818cf8, #c084fc); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 24px; text-align: center; }}
  .title-sub {{ font-size: 36px; font-weight: 300; color: #94a3b8; text-align: center; margin-bottom: 16px; }}
  .title-label {{ font-size: 24px; color: #64748b; letter-spacing: 4px; margin-top: 32px; }}
  .problem-box {{ background: rgba(30,41,59,0.8); border: 2px solid rgba(56,189,248,0.3); border-radius: 24px; padding: 60px 80px; text-align: center; }}
  .problem-text {{ font-size: 56px; font-weight: 700; color: #f1f5f9; line-height: 1.5; }}
  .problem-label {{ font-size: 28px; color: #38bdf8; margin-bottom: 32px; font-weight: 500; letter-spacing: 2px; }}
  .step-title {{ font-size: 48px; font-weight: 700; margin-bottom: 48px; color: #f1f5f9; display: flex; align-items: center; gap: 20px; align-self: flex-start; }}
  .step-icon {{ width: 64px; height: 64px; border-radius: 16px; display: flex; align-items: center; justify-content: center; font-size: 32px; font-weight: 900; color: white; flex-shrink: 0; }}
  .icon-blue {{ background: linear-gradient(135deg, #0ea5e9, #38bdf8); }}
  .icon-purple {{ background: linear-gradient(135deg, #7c3aed, #a78bfa); }}
  .icon-green {{ background: linear-gradient(135deg, #059669, #34d399); }}
  .icon-amber {{ background: linear-gradient(135deg, #d97706, #fbbf24); }}
  .icon-rose {{ background: linear-gradient(135deg, #e11d48, #fb7185); }}
  .step-body {{ font-size: 36px; line-height: 1.8; color: #cbd5e1; align-self: flex-start; width: 100%; }}
  .math-block {{ background: rgba(15,23,42,0.6); border-left: 4px solid #38bdf8; padding: 32px 48px; margin: 24px 0; border-radius: 0 16px 16px 0; font-size: 40px; font-family: 'Georgia','Times New Roman',serif; color: #e2e8f0; line-height: 1.8; }}
  .math-block.highlight {{ border-left-color: #34d399; background: rgba(5,150,105,0.1); }}
  .math-block.cancel {{ border-left-color: #f87171; background: rgba(239,68,68,0.08); }}
  .hl-blue {{ color: #38bdf8; font-weight: 700; }}
  .hl-green {{ color: #34d399; font-weight: 700; }}
  .hl-amber {{ color: #fbbf24; font-weight: 700; }}
  .hl-rose {{ color: #fb7185; font-weight: 700; }}
  .hl-purple {{ color: #a78bfa; font-weight: 700; }}
  .answer-box {{ background: linear-gradient(135deg,rgba(5,150,105,0.2),rgba(16,185,129,0.1)); border: 2px solid rgba(52,211,153,0.4); border-radius: 24px; padding: 60px 100px; text-align: center; }}
  .answer-number {{ font-size: 120px; font-weight: 900; background: linear-gradient(135deg,#34d399,#6ee7b7); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .answer-label {{ font-size: 32px; color: #6ee7b7; margin-bottom: 20px; letter-spacing: 4px; }}
  .summary-card {{ background: rgba(30,27,75,0.5); border: 1px solid rgba(167,139,250,0.3); border-radius: 20px; padding: 48px 64px; width: 100%; }}
  .summary-card h3 {{ font-size: 32px; color: #a78bfa; margin-bottom: 24px; }}
  .summary-card p {{ font-size: 30px; line-height: 1.8; color: #c4b5fd; }}
  .tip-box {{ background: rgba(251,191,36,0.1); border: 1px solid rgba(251,191,36,0.3); border-radius: 16px; padding: 32px 48px; margin-top: 32px; width: 100%; }}
  .tip-box p {{ font-size: 28px; color: #fde68a; line-height: 1.6; }}
  .bottom-bar {{ position: absolute; bottom: 0; left: 0; right: 0; height: 6px; background: linear-gradient(90deg,#38bdf8,#818cf8,#c084fc,#34d399); }}
</style>
</head>
<body>
{slides}
</body>
</html>'''

ICONS = ['icon-blue', 'icon-purple', 'icon-amber', 'icon-green', 'icon-rose']


def gen(data):
    q = data['q_num']
    topic = data['topic']
    problem = data['problem_text']
    answer = data['answer']
    steps = data['steps']
    summary_when = data['summary_when']
    summary_how = data['summary_how']
    tip = data['tip']
    header = data.get('header', f'第{q}题')
    exam_title = data.get('exam_title', '数学教学视频')
    grade_label = data.get('grade_label', '— 适合小学生 —')
    total_slides = len(steps) + 4  # title + problem + N steps + answer + summary

    slides = []

    # Slide 1: Title
    slides.append(f'''<div class="slide slide-title" id="slide1">
  <div class="title-label">{exam_title}</div>
  <div class="title-main">{header} · {topic}</div>
  <div class="title-sub">{data.get('subtitle', '')}</div>
  <div class="title-label" style="color:#475569;margin-top:48px;">{grade_label}</div>
  <div class="bottom-bar"></div>
</div>''')

    # Slide 2: Problem
    slides.append(f'''<div class="slide slide-problem" id="slide2">
  <div class="badge">{header}</div>
  <div class="slide-number">02 / {total_slides:02d}</div>
  <div class="problem-label">📝 题 目</div>
  <div class="problem-box">
    <div class="problem-text">{problem}</div>
  </div>
  <div class="bottom-bar"></div>
</div>''')

    # Step slides
    for i, step in enumerate(steps):
        sn = i + 3
        icon = ICONS[i % len(ICONS)]
        slides.append(f'''<div class="slide slide-step" id="slide{sn}">
  <div class="badge">{step["badge"]}</div>
  <div class="slide-number">{sn:02d} / {total_slides:02d}</div>
  <div class="step-title">
    <div class="step-icon {icon}">{i + 1}</div>
    {step["title"]}
  </div>
  <div class="step-body">
    {step["content_html"]}
  </div>
  <div class="bottom-bar"></div>
</div>''')

    # Answer slide
    ans_n = len(steps) + 3
    slides.append(f'''<div class="slide slide-answer" id="slide{ans_n}">
  <div class="badge" style="background:rgba(52,211,153,0.15);border-color:rgba(52,211,153,0.3);color:#34d399;">✅ 答案</div>
  <div class="slide-number">{ans_n:02d} / {total_slides:02d}</div>
  <div class="answer-box">
    <div class="answer-label">最 终 答 案</div>
    <div class="answer-number">{answer}</div>
  </div>
  <div class="bottom-bar"></div>
</div>''')

    # Summary slide
    sum_n = len(steps) + 4
    slides.append(f'''<div class="slide slide-summary" id="slide{sum_n}">
  <div class="badge" style="background:rgba(167,139,250,0.15);border-color:rgba(167,139,250,0.3);color:#a78bfa;">📚 技巧总结</div>
  <div class="slide-number">{sum_n:02d} / {total_slides:02d}</div>
  <div class="step-title" style="margin-bottom:32px;">
    <div class="step-icon" style="background:linear-gradient(135deg,#7c3aed,#a78bfa);">★</div>
    {topic}
  </div>
  <div class="summary-card">
    <h3>什么时候用？</h3>
    <p>{summary_when}</p>
  </div>
  <div class="summary-card" style="margin-top:24px;">
    <h3>怎么用？</h3>
    <p>{summary_how}</p>
  </div>
  <div class="tip-box">
    <p>🧠 {tip}</p>
  </div>
  <div class="bottom-bar"></div>
</div>''')

    return TEMPLATE.format(slides='\n\n'.join(slides)), total_slides


if __name__ == '__main__':
    infile = sys.argv[1]
    outfile = sys.argv[2]
    with open(infile) as f:
        data = json.load(f)
    result, total = gen(data)
    with open(outfile, 'w') as f:
        f.write(result)
    print(f"Generated {total} slides -> {outfile}")
