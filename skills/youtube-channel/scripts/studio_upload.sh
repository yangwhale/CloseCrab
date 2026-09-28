#!/usr/bin/env bash
# YouTube Studio 网页上传（绕开未审核 API 的私享锁）：上传 → 标题/简介 → 非儿童 → 公开发布 → 打印视频链接
# 用法：yt_studio_upload.sh 文件.mp4 "标题" 简介.txt
set -euo pipefail
A=~/.claude/skills/browser-cli/scripts/ab.local; F0="$1"
# ⛔ 原文件直接传偶发「File unreadable」，拷一份新文件再传就好（原因未明）
mkdir -p ${UP_TMP:-/tmp/yt-up}; F=${UP_TMP:-/tmp/yt-up}/$(basename "$F0"); cp "$F0" "$F"; T="$2"; D="$3"
$A open "https://studio.youtube.com/channel/${YT_CHANNEL_ID:?需要设 YT_CHANNEL_ID}/videos/upload?d=ud" >/dev/null; sleep 10
$A eval "[...document.querySelectorAll('ytcp-dialog')].forEach(d=>{const b=[...d.querySelectorAll('button,ytcp-button')].find(b=>/Continue/.test(b.innerText));b&&b.click()})" >/dev/null
$A upload "input[type=file]" "$F" >/dev/null; sleep 20
DJ=$(python3 -c "import json,sys;print(json.dumps(open(sys.argv[1]).read()))" "$D"); TJ=$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$T")
$A eval "(()=>{const set=(label,t)=>{const el=[...document.querySelectorAll('ytcp-uploads-dialog [contenteditable=true]')].find(e=>(e.getAttribute('aria-label')||'').startsWith(label));el.focus();document.execCommand('selectAll');document.execCommand('insertText',false,t);return el.innerText.length};return [set('Add a title',$TJ),set('Tell viewers',$DJ)]})()"
$A eval "(()=>{const x=document.querySelector('ytcp-uploads-dialog tp-yt-paper-radio-button[name=VIDEO_MADE_FOR_KIDS_NOT_MFK]');x.scrollIntoView();x.click();return x.getAttribute('aria-checked')})()"
for i in 1 2 3; do $A eval "document.querySelector('ytcp-uploads-dialog #next-button').click()" >/dev/null; sleep 3; done
$A eval "(()=>{const r=document.querySelector('ytcp-uploads-dialog tp-yt-paper-radio-button[name=PUBLIC]');r.click();return r.getAttribute('aria-checked')})()"
# 等上传走完再发布（大文件）
for i in $(seq 1 240); do
  s=$($A eval "document.querySelector('ytcp-uploads-dialog')?.innerText.match(/Upload(ing)? [^\n]*/)?.[0]||''" | tr -d '"')
  case "$s" in *complete*|*Complete*) break;; esac; sleep 15
done
URL=$($A eval "document.querySelector('ytcp-uploads-dialog')?.innerText.match(/https:\/\/(youtu\.be|youtube\.com\/shorts)\/[\w-]+/)?.[0]" | tr -d '"')
$A eval "document.querySelector('ytcp-uploads-dialog #done-button').click()" >/dev/null; sleep 8
echo "$URL"
