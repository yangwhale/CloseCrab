#!/usr/bin/env bash
# 在 B站视频下发一条章节时间点评论并置顶（进度条分段章节编辑器自动化不了，用它顶上；评论里的时间点可点击跳转）
# 用法：bili_pin_chapters.sh BV号 章节文件 课件页名
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"; BV="$1"; CH="$2"; PAGE="$3"
MSG=$(python3 -c "
import json,sys
ch=open('$CH').read().strip().split('\n')
def f(l):
    t,title=l.split(' ',1); p=[int(x) for x in t.split(':')]
    s=p[0]*3600+p[1]*60+p[2] if len(p)==3 else p[0]*60+p[1]
    t='%d:%02d:%02d'%(s//3600,s%3600//60,s%60) if s>=3600 else '%02d:%02d'%(s//60,s%60)
    return t+' '+title
print(json.dumps('📌 章节（点时间可跳转）\n'+'\n'.join(f(l) for l in ch)+'\n\n📖 课件与讲义免费开源：gist.higcp.com/Courses/WebPages/$PAGE'))")
$A open "https://www.bilibili.com/video/$BV" >/dev/null; sleep 5
$A eval "(async()=>{const csrf=document.cookie.match(/bili_jct=([^;]+)/)[1];const v=await (await fetch('https://api.bilibili.com/x/web-interface/view?bvid=$BV',{credentials:'include'})).json();const aid=v.data.aid;const r=await (await fetch('https://api.bilibili.com/x/v2/reply/add',{method:'POST',credentials:'include',body:new URLSearchParams({type:1,oid:aid,message:$MSG,plat:1,csrf})})).json();if(r.code!==0)return 'add '+JSON.stringify(r);await new Promise(s=>setTimeout(s,6000));const t=await (await fetch('https://api.bilibili.com/x/v2/reply/top',{method:'POST',credentials:'include',body:new URLSearchParams({type:1,oid:aid,rpid:r.data.rpid,action:1,csrf})})).json();return 'add ok, top '+t.code+' '+t.message})()"
