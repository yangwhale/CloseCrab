#!/usr/bin/env bash
# 点「立即投稿」并轮询到「稿件投递成功」。实测两种页面各认一种点法：
#   编辑页（type=edit）认快照 ref 点击，坐标点击静默无效；
#   新投稿页认「滚到可见 → 鼠标坐标点击」，ref 点击静默无效（连请求都不发）。
# 所以先 ref、没成功再坐标，两次都失败才报。
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"
ok(){ for i in $(seq 1 8); do sleep 1
  M=$($A eval "(document.body.innerText.match(/稿件投递成功|[^\n]*(失败|频繁)[^\n]*/)||[''])[0]" | tr -d '"')
  [ -n "$M" ] && { echo "$M"; return 0; }; done; return 1; }
R=$($A snapshot -i | grep -o 'generic "立即投稿" \[ref=e[0-9]*' | grep -o 'e[0-9]*$' | tail -1)
[ -n "$R" ] && $A click @$R >/dev/null && ok && exit 0
P=$($A eval "(()=>{const e=[...document.querySelectorAll('span,div')].filter(e=>e.offsetParent&&e.innerText&&e.innerText.trim()==='立即投稿').pop();e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
sleep 1; $A mouse move $P >/dev/null; $A mouse down >/dev/null; $A mouse up >/dev/null
ok || echo "?（没看到成功提示，去内容管理确认）"
