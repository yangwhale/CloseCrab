#!/usr/bin/env bash
# 点「立即投稿」：用快照里的 ref 点（坐标点击在编辑页实测会静默不生效），轮询到「稿件投递成功」为止
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"
R=$($A snapshot -i | grep -o 'generic "立即投稿" \[ref=e[0-9]*' | grep -o 'e[0-9]*$' | tail -1)
$A click @$R >/dev/null
for i in $(seq 1 10); do sleep 1
  M=$($A eval "(document.body.innerText.match(/稿件投递成功|恭喜[^\n]*|[^\n]*(失败|频繁)[^\n]*/)||[''])[0]" | tr -d '"')
  [ -n "$M" ] && { echo "$M"; exit 0; }
done; echo "?（没看到成功提示，去内容管理确认）"
