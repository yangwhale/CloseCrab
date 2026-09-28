#!/usr/bin/env bash
# B站 官方网页投稿（唯一投稿通道）：视频 → 等上传完 → 简介 → 标签 → 创作声明 → 封面 → 标题 → 立即投稿
# 用法：web_upload.sh 视频.mp4 封面.jpg "标题" 简介.txt "标签1,标签2,..." [创作声明，默认 含AI生成内容]
# 前提：本机 Chrome（browser-cli 的 ab.local）已登录账号。文件名用纯 ASCII。
set -uo pipefail
A="timeout 40 $HOME/.claude/skills/browser-cli/scripts/ab.local"; D=$(dirname "$0")
# ⛔ 文件一律转绝对路径：ab 的 upload 按它自己的工作目录解析相对路径，封面给相对路径会让页面卡死
V=$(realpath "$1") C=$(realpath "$2") T="$3" DESC=$(realpath "$4") TAGS="$5" DECL="${6:-含AI生成内容}"
for f in "$V" "$C" "$DESC"; do [ -f "$f" ] || { echo "找不到文件：$f"; exit 1; }; done
J(){ python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$1"; }
$A open "https://member.bilibili.com/platform/upload/video/frame" >/dev/null; sleep 8
$A upload "input[type=file]" "$V" >/dev/null
# ⛔ 设完文件后页面整段读文件算哈希，期间任何 CDP 调用都超时，像冻死 —— 先闭嘴等，再轮询「上传完成」
MB=$(( $(stat -c %s "$V") / 1048576 )); W=$(( MB / 4 + 30 )); echo "等哈希 ${W}s（${MB} MB）"; sleep $W
for i in $(seq 1 120); do
  s=$($A eval "/上传完成/.test(document.body.innerText)" 2>/dev/null); [ "$s" = true ] && break; sleep 10
done; [ "$s" = true ] || { echo "上传没完成（页面可能真冻了：browser-cli 的 local-chrome.sh 重启）"; exit 1; }
$A eval "[...document.querySelectorAll('*')].filter(e=>e.offsetParent&&e.innerText&&e.innerText.trim()==='知道了').forEach(x=>x.click())" >/dev/null
# 简介
$A eval "(()=>{const e=document.querySelector('[contenteditable=true]');e.focus();document.execCommand('selectAll');document.execCommand('insertText',false,$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1]).read()))" "$DESC"));return e.innerText.length})()" >/dev/null
# 默认标签（生活记录/记录/新人…）全删：关闭图标上派发鼠标事件，一个一个来
for i in $(seq 1 12); do
  r=$($A eval "(()=>{const c=document.querySelector('.label-item-v2-container');if(!c)return 'none';const x=c.querySelector('.close');['mouseenter','mouseover','mousedown','mouseup','click'].forEach(t=>{x.dispatchEvent(new MouseEvent(t,{bubbles:true}));c.dispatchEvent(new MouseEvent(t,{bubbles:false}))});return 'ok'})()" | tr -d '"')
  [ "$r" = none ] && break; sleep 0.8
done
# 加标签：JS 设 value + input + Enter；每个之间停一下，否则只进第一个
IFS=, read -ra TG <<< "$TAGS"
for tg in "${TG[@]}"; do
  $A eval "(()=>{const i=document.querySelector('input[placeholder*=\"创建标签\"]');i.focus();i.value=$(J "$tg");i.dispatchEvent(new Event('input',{bubbles:true}));['keydown','keypress','keyup'].forEach(k=>i.dispatchEvent(new KeyboardEvent(k,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true})));return 1})()" >/dev/null; sleep 0.8
done
"$D/set_tags_decl.sh" "" "" "$DECL" >/dev/null
# 封面：「添加封面」弹窗 → 双比例同步（默认已勾）→ 图片 input → 完成。别点上传区（弹系统文件框会冻住）
xy(){ $A eval "(()=>{const e=[...document.querySelectorAll('*')].filter(e=>e.innerText&&e.innerText.trim()==='$1'&&e.offsetParent).pop();if(!e)return '';e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"'; }
click(){ [ -n "$1" ] && { $A mouse move $1 >/dev/null; $A mouse down >/dev/null; $A mouse up >/dev/null; }; }
click "$(xy 添加封面)"; sleep 3; click "$(xy 知道了)"; sleep 1
$A eval "(()=>{const l=document.querySelector('.sync-checkbox');if(l&&!l.className.includes('checked'))l.click();return 1})()" >/dev/null
$A upload "input[type=file][accept^='image']" "$C" >/dev/null; sleep 6
click "$(xy 完成)"; sleep 3
# 标题最后设（上传完成那一刻会被文件名覆盖）
"$D/set_title.sh" "$T"
$A eval "'tags='+[...document.querySelectorAll('.label-item-v2-content')].map(e=>e.innerText).join(',')+' | 声明='+document.querySelector('input[placeholder*=\"创作声明\"]').value+' | 简介 '+document.querySelector('[contenteditable=true]').innerText.length+' 字'"
"$D/submit_click.sh"
