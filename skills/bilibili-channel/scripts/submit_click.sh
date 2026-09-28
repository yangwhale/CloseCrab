#!/usr/bin/env bash
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"
P=$($A eval "(()=>{const e=[...document.querySelectorAll('*')].filter(e=>e.innerText&&e.innerText.trim()==='立即投稿'&&e.offsetParent).pop();e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();return Math.round(r.x+r.width/2)+' '+Math.round(r.y+r.height/2)})()" | tr -d '"')
$A mouse move $P >/dev/null; $A mouse down >/dev/null; $A mouse up >/dev/null; sleep 8
$A eval "(document.body.innerText.match(/稿件投递成功|恭喜[^\n]*/)||['?'])[0]"
