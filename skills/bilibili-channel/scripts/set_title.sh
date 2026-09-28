#!/usr/bin/env bash
# 在已打开的投稿/编辑页里设标题：必须 click→Ctrl+A→Delete→type→Tab，字数计数器变了才算写进表单模型
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"; T="$1"
R=$($A snapshot -i | grep -o 'textbox "请输入稿件标题" \[ref=e[0-9]*' | grep -o 'e[0-9]*$')
$A click @$R >/dev/null; $A press Control+a >/dev/null; $A press Delete >/dev/null; $A type @$R "$T" >/dev/null; $A press Tab >/dev/null; sleep 1
$A eval "(()=>{const i=document.querySelector('input[placeholder=\"请输入稿件标题\"]');return i.value.length+' chars, counter '+i.closest('div').parentElement.innerText.slice(0,10)})()"
