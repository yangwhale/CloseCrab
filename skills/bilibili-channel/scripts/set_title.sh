#!/usr/bin/env bash
# 在已打开的投稿/编辑页里设标题。键盘 Ctrl+A 在编辑页经常选不中（会接在旧标题后面），
# 所以先用 JS select() 全选再键入，最后补发 input 事件 —— 字数计数器变了才算写进表单模型。
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"; T="$1"
R=$($A snapshot -i | grep -o 'textbox "请输入稿件标题" \[ref=e[0-9]*' | grep -o 'e[0-9]*$')
$A click @$R >/dev/null
$A eval "(()=>{const i=document.querySelector('input[placeholder=\"请输入稿件标题\"]');i.focus();i.select();return 1})()" >/dev/null
$A type @$R "$T" >/dev/null; $A press Tab >/dev/null; sleep 0.5
$A eval "(()=>{const i=document.querySelector('input[placeholder=\"请输入稿件标题\"]');i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));return 1})()" >/dev/null; sleep 1
$A eval "(()=>{const i=document.querySelector('input[placeholder=\"请输入稿件标题\"]');const c=[...document.querySelectorAll('*')].find(e=>e.children.length==0&&/^\s*\d+\s*\/\s*80\s*$/.test(e.textContent||''));return i.value+' | counter '+(c?c.textContent.trim():'?')})()"
