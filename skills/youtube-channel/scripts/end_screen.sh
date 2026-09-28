#!/usr/bin/env bash
# 片尾画面：模板「1 视频 + 1 播放列表 + 订阅」，视频=下一讲（给 NEXT 标题关键字，如 第3讲；空则保留 Best for viewer）
V="$1"; NEXT="${2:-}"
A="timeout 40 $HOME/.claude/skills/browser-cli/scripts/ab.local"; C(){ $A mouse move $1 $2 >/dev/null; $A mouse down >/dev/null; $A mouse up >/dev/null; }
$A open "https://studio.youtube.com/video/$V/edit" >/dev/null; sleep 10
P=$($A eval "(()=>{const e=[...document.querySelectorAll('span')].filter(e=>e.offsetParent&&e.innerText.trim()==='End screen').pop();e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"'); C $P; sleep 10
$A eval "(()=>{const h=[...document.querySelectorAll('ytve-modal-host')].find(d=>d.offsetParent);const e=[...h.querySelectorAll('*')].filter(e=>e.children.length===0&&e.innerText&&e.innerText.trim()==='1 video, 1 playlist, 1 subscribe').pop();e.scrollIntoView({block:'center'});return 1})()" >/dev/null; sleep 1
C 644 390; sleep 3; C 790 618; sleep 2; C 667 255; sleep 3; C 327 250; sleep 2
if [ -n "$NEXT" ]; then C 790 590; sleep 2; C 330 408; sleep 3
  P=$($A eval "(()=>{const d=[...document.querySelectorAll('ytcp-dialog,tp-yt-paper-dialog')].filter(d=>d.offsetParent&&/Choose specific video/.test(d.innerText)).pop();const e=[...d.querySelectorAll('*')].filter(e=>e.children.length===0&&(e.innerText||'').includes('$NEXT')).pop();const r=e.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"'); C $P; sleep 2; fi
$A eval "(()=>{const h=[...document.querySelectorAll('ytve-modal-host')].find(d=>d.offsetParent);return (h.innerText.match(/(Subscribe|Video|Playlist): [^\n]{0,30}/g)||[]).slice(-3).join(' / ')+' '+(h.innerText.match(/Please choose[^\n]*/)||[''])[0]})()"
C 1140 78; sleep 4
$A eval "[...document.querySelectorAll('ytve-modal-host')].some(d=>d.offsetParent)?'still open':'saved'"
