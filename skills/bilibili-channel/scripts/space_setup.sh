#!/usr/bin/env bash
# 个人空间：公告 / 置顶视频 / 代表作 / 系列列表（在已登录的本机 Chrome 里用页面 fetch 调接口，csrf 取 bili_jct）
# 用法：space_setup.sh notice "公告文字(≤150字)"
#       space_setup.sh top BV号 "置顶理由"
#       space_setup.sh masterpiece BV号 "推荐语"        （最多 3 个）
#       space_setup.sh series MID "列表名" "简介" BV1,BV2,...   （系列列表无等级门槛；「合集」要权益 Lv2）
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"
$A eval "location.hostname" | grep -q bilibili || { $A open "https://space.bilibili.com/" >/dev/null; sleep 6; }
J(){ python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "$1"; }
PRE="const csrf=document.cookie.match(/bili_jct=([^;]+)/)[1];const P=(u,b)=>fetch(u,{method:'POST',credentials:'include',body:new URLSearchParams({...b,csrf})}).then(r=>r.json());const aid=async bv=>(await (await fetch('https://api.bilibili.com/x/web-interface/view?bvid='+bv,{credentials:'include'})).json()).data.aid;"
case "$1" in
  notice) $A eval "(async()=>{$PRE return JSON.stringify(await P('https://api.bilibili.com/x/space/notice/set',{notice:$(J "$2")}))})()";;
  top) $A eval "(async()=>{$PRE return JSON.stringify(await P('https://api.bilibili.com/x/space/top/arc/set',{aid:await aid('$2'),reason:$(J "$3")}))})()";;
  masterpiece) $A eval "(async()=>{$PRE return JSON.stringify(await P('https://api.bilibili.com/x/space/masterpiece/add',{aid:await aid('$2'),reason:$(J "$3")}))})()";;
  series) $A eval "(async()=>{$PRE const aids=[];for(const bv of '$5'.split(','))aids.push(await aid(bv));const r=await fetch('https://api.bilibili.com/x/series/series/createAndAddArchives?csrf='+csrf,{method:'POST',credentials:'include',headers:{'content-type':'application/x-www-form-urlencoded'},body:new URLSearchParams({mid:'$2',name:$(J "$3"),keywords:'',description:$(J "$4"),aids:aids.join(',')})});return JSON.stringify(await r.json())})()";;
  *) sed -n 2,8p "$0";;
esac
