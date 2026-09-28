import sys, base64
from playwright.sync_api import sync_playwright
import os, html as H
# 人像版封面（频道标准封面，片头也用它）：
#   COVER_PORTRAIT=抠好图的半身像.png COVER_NAME="名字" COVER_ROLE="头衔" COVER_BIO="一句话专长" [COVER_SERIES=系列名] [COVER_SCALE=2 出 4K]
#   make_cover_portrait.py 输出.png "专题五" "并行策略" "钩子第一行|钩子第二行"      标题里的 | 换行
out, num, title, hook = sys.argv[1:5]
E = lambda k, d="": H.escape(os.environ.get(k, d))
img = base64.b64encode(open(os.environ["COVER_PORTRAIT"], 'rb').read()).decode()
hook = "<br>".join(hook.split("|"))
num_top = 270 if "|" in title else 290
hook_top = 930 if "|" in title else 720
html = f"""<html><head><style>
body{{margin:0;width:1920px;height:1080px;font-family:'Noto Sans CJK SC','Noto Sans SC',sans-serif;
background:linear-gradient(135deg,#ffffff 0%,#eef3fd 100%);position:relative;overflow:hidden}}
.disc{{position:absolute;right:-120px;top:120px;width:1020px;height:1020px;border-radius:50%;
background:radial-gradient(circle at 40% 35%,#8ab4f8 0%,#4285f4 55%,#1a73e8 100%)}}
.me{{position:absolute;right:40px;bottom:22px;height:1010px}}
.series{{position:absolute;left:120px;top:90px;font-size:40px;color:#5f6368;font-weight:500;letter-spacing:2px}}
.badge{{position:absolute;left:120px;top:160px;font-size:44px;color:#fff;background:#ea4335;padding:10px 30px;border-radius:14px;font-weight:800}}
.num{{position:absolute;left:120px;top:{num_top}px;font-size:96px;color:#1a73e8;font-weight:900}}
.title{{position:absolute;left:112px;top:{num_top+120}px;font-size:200px;color:#202124;font-weight:900;line-height:1.05;white-space:nowrap;display:inline-block}}
.hook{{position:absolute;left:120px;top:{hook_top}px;font-size:64px;width:1100px;color:#202124;font-weight:800;line-height:1.35;
border-left:14px solid #fbbc04;padding-left:32px}}
.tag{{position:absolute;right:60px;bottom:70px;background:rgba(255,255,255,.96);border-radius:22px;padding:22px 34px;
box-shadow:0 8px 28px rgba(0,0,0,.18);border-left:12px solid #1a73e8}}
.tag b{{display:block;font-size:58px;color:#202124;font-weight:900;letter-spacing:1px}}
.tag em{{font-style:normal;font-size:30px;color:#fff;background:#1a73e8;border-radius:10px;padding:4px 14px;margin-right:18px;vertical-align:middle;position:relative;top:-6px}}
.tag span{{display:block;font-size:34px;color:#1a73e8;font-weight:800;margin-top:4px}}
.tag i{{display:block;font-style:normal;font-size:28px;color:#5f6368;font-weight:600;margin-top:6px}}
.bar{{position:absolute;left:0;bottom:0;height:22px;width:100%;background:linear-gradient(90deg,#4285f4 0 25%,#ea4335 25% 50%,#fbbc04 50% 75%,#34a853 75%)}}
</style></head><body>
<div class="disc"></div><img class="me" src="data:image/png;base64,{img}">
<div class="series">{E("COVER_SERIES", "现代 AI 加速器 GPU / TPU 系统课程")}</div><div class="badge">免费开源课</div>
<div class="num">{num}</div><div class="title">{"<br>".join(title.split("|"))}</div><div class="hook">{hook}</div><div class="tag"><b><em>作者</em>{E("COVER_NAME")}</b><span>{E("COVER_ROLE")}</span><i>{E("COVER_BIO")}</i></div><div class="bar"></div></body></html>"""
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1920, "height": 1080}, device_scale_factor=float(__import__("os").environ.get("COVER_SCALE", "1"))); pg.set_content(html); pg.wait_for_timeout(400)
    # 标题不压到人像：右边界 1200，超了就缩
    pg.evaluate("()=>{const t=document.querySelector('.title');let f=200;while(t.getBoundingClientRect().right>1040&&f>90){f-=6;t.style.fontSize=f+'px'};if(t.querySelector('br')&&f>150){t.style.fontSize='150px'};const h=document.querySelector('.hook');h.style.top=(t.getBoundingClientRect().bottom+40)+'px'}")
    pg.screenshot(path=out); b.close()
