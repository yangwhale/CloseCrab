#!/usr/bin/env bash
# 置顶 YouTube 评论：yt_pin.sh 视频ID 评论ID
A="timeout 40 $HOME/.claude/skills/browser-cli/scripts/ab.local"
$A open "https://www.youtube.com/watch?v=$1&lc=$2" >/dev/null; sleep 8; $A eval "window.scrollTo(0,900)" >/dev/null; sleep 5
$A eval "(()=>{const t=document.querySelector('ytd-comment-thread-renderer');if(!t)return 'no thread';if(/Pinned by/.test(t.innerText))return 'already pinned';t.querySelector('#action-menu button, ytd-menu-renderer button').click();return 'menu'})()" 
sleep 1.5
$A eval "(()=>{const i=[...document.querySelectorAll('ytd-menu-service-item-renderer, tp-yt-paper-item')].filter(e=>e.offsetParent).find(e=>e.innerText.trim()==='Pin');if(!i)return 'no pin item';i.click();return 'pin'})()"
sleep 1.5
$A eval "(()=>{const p=[...document.querySelectorAll('yt-confirm-dialog-renderer button, tp-yt-paper-dialog button')].filter(e=>e.offsetParent).find(x=>/Pin/i.test(x.innerText));if(p){p.click();return 'confirmed'}return 'no confirm'})()"
sleep 3
$A open "https://www.youtube.com/watch?v=$1" >/dev/null; sleep 8; $A eval "window.scrollTo(0,900)" >/dev/null; sleep 5
$A eval "(document.querySelector('ytd-comment-thread-renderer')?.innerText||'').slice(0,40)"
