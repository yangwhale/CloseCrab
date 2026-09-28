import sys
from playwright.sync_api import sync_playwright
# 用法：make.py 输出.png "专题五" "并行策略" "副标题" "要点1|要点2|要点3"
out, num, title, sub, pts = sys.argv[1:6]
chips = "".join('<span class="chip">%s</span>' % p for p in pts.split("|"))
html = f"""<html><head><style>
body{{margin:0;width:1920px;height:1080px;font-family:'Noto Sans CJK SC','Noto Sans SC',sans-serif;
background:linear-gradient(135deg,#ffffff 0%,#eef3fd 100%);position:relative;overflow:hidden}}
.dots{{position:absolute;left:280px;top:110px;display:flex;gap:18px}}.dots i{{width:30px;height:30px;border-radius:50%;display:block}}
.series{{position:absolute;left:280px;top:180px;font-size:44px;color:#5f6368;font-weight:500;letter-spacing:2px}}
.num{{position:absolute;left:280px;top:270px;font-size:80px;color:#1a73e8;font-weight:900}}
.title{{position:absolute;left:272px;top:370px;font-size:190px;color:#202124;font-weight:900;letter-spacing:4px;line-height:1.05}}
.sub{{position:absolute;left:280px;top:640px;font-size:52px;width:1360px;color:#3c4043;font-weight:700}}
.chips{{position:absolute;left:280px;top:790px;display:flex;gap:22px;flex-wrap:wrap;width:1360px}}
.chip{{font-size:44px;padding:14px 32px;border-radius:40px;background:#e8f0fe;color:#174ea6;font-weight:700}}
.badge{{position:absolute;right:280px;top:110px;font-size:40px;color:#fff;background:#ea4335;padding:12px 30px;border-radius:14px;font-weight:800}}
.bar{{position:absolute;left:0;bottom:0;height:22px;width:100%;background:linear-gradient(90deg,#4285f4 0 25%,#ea4335 25% 50%,#fbbc04 50% 75%,#34a853 75%)}}
</style></head><body>
<div class="dots"><i style="background:#4285f4"></i><i style="background:#ea4335"></i><i style="background:#fbbc04"></i><i style="background:#34a853"></i></div>
<div class="badge">免费开源课</div>
<div class="series">现代 AI 加速器 GPU / TPU 系统课程</div>
<div class="num">{num}</div><div class="title">{title}</div><div class="sub">{sub}</div>
<div class="chips">{chips}</div><div class="bar"></div></body></html>"""
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1920, "height": 1080}); pg.set_content(html); pg.wait_for_timeout(300)
    pg.evaluate("()=>{const t=document.querySelector('.title');t.style.whiteSpace='nowrap';let f=190;while(t.getBoundingClientRect().right>1640&&f>90){f-=6;t.style.fontSize=f+'px'}}")
    pg.screenshot(path=out); b.close()
