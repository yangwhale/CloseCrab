#!/usr/bin/env bash
# B站进度条分段章节：章节管理页 → 播放器 iframe「章节文本编辑器」→ 粘贴 HH:MM:SS 标题 → 转换 → 保存（提交后要审核才显示）
# 用法：bili_chapters.sh BV号 章节文件（每行「m:ss 标题」或「h:mm:ss 标题」）
BV="$1"; CH="$2"
A="timeout 40 $HOME/.claude/skills/browser-cli/scripts/ab.local"
$A open "https://member.bilibili.com/platform/upload-manager/article/chapterSetting?bvid=$BV" >/dev/null; sleep 8
for i in $(seq 1 24); do r=$(python3 $(dirname "$(readlink -f "$0")")/cdp_frame.py "bvid=$BV" "web/player" "document.body&&document.body.innerText.includes('章节数')?'ready':'wait'" 2>/dev/null); [ "$r" = '"ready"' ] && break; sleep 5; done
TXT=$(python3 -c "
import json
L=[]
for l in open('$CH').read().strip().split('\n'):
    t,title=l.split(' ',1); p=[int(x) for x in t.split(':')]; s=p[0]*3600+p[1]*60+p[2] if len(p)==3 else p[0]*60+p[1]
    L.append('%02d:%02d:%02d %s'%(s//3600,s%3600//60,s%60,title[:30]))
print(json.dumps('\n'.join(L)))")
python3 $(dirname "$(readlink -f "$0")")/cdp_frame.py "bvid=$BV" "web/player" "(async()=>{const S=ms=>new Promise(r=>setTimeout(r,ms));document.querySelector('.chapter-manager-text-editor-btn').click();await S(1500);const ta=document.querySelector('textarea.text-area');const set=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;set.call(ta,$TXT);ta.dispatchEvent(new Event('input',{bubbles:true}));await S(500);[...document.querySelectorAll('.modal-footer *')].find(e=>e.innerText&&e.innerText.trim()==='转换成章节列表').click();await S(2000);const save=[...document.querySelectorAll('button,[class*=btn]')].filter(e=>e.offsetParent&&(e.innerText||'').trim()==='保存').pop();save.click();await S(3000);const t=document.body.innerText;return (t.match(/章节数：\d+/)||['?'])[0]+' '+(t.match(/提交成功|失败[^\n]*/)||['?'])[0]})()"
