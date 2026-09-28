#!/usr/bin/env bash
# 编辑页：删标签 / 加标签 / 设创作声明。标签框和声明下拉都不认 ab 的键盘和点击，必须用 JS 派发事件
# 用法：set_tags_decl.sh "要删的标签(可空)" "要加的标签1,标签2(可空)" ["含AI生成内容"]
A="timeout 30 $HOME/.claude/skills/browser-cli/scripts/ab.local"
DEL=$(python3 -c "import json,sys;print(json.dumps([t for t in sys.argv[1].split(',') if t]))" "$1")
ADD=$(python3 -c "import json,sys;print(json.dumps([t for t in sys.argv[1].split(',') if t]))" "$2")
DECL=$(python3 -c "import json,sys;print(json.dumps(sys.argv[1]))" "${3:-}")
$A eval "(()=>{for(const d of $DEL){const c=[...document.querySelectorAll('.label-item-v2-container')].find(e=>e.innerText.trim()===d);if(c){const x=c.querySelector('.close');['mouseover','mousedown','mouseup','click'].forEach(t=>x.dispatchEvent(new MouseEvent(t,{bubbles:true})))}}
const i=document.querySelector('input[placeholder*=\"创建标签\"]');for(const a of $ADD){i.focus();i.value=a;i.dispatchEvent(new Event('input',{bubbles:true}));['keydown','keypress','keyup'].forEach(t=>i.dispatchEvent(new KeyboardEvent(t,{key:'Enter',code:'Enter',keyCode:13,which:13,bubbles:true})))}
if($DECL){const d=document.querySelector('input[placeholder*=\"创作声明\"]');let w=d;for(let k=0;k<4;k++)w=w.parentElement;d.click();const li=[...w.querySelectorAll('li,[class*=item]')].find(e=>e.innerText.trim()===$DECL);li&&li.click()}return 1})()" >/dev/null; sleep 1
$A eval "'tags='+[...document.querySelectorAll('.label-item-v2-content')].map(e=>e.innerText).join(',')+' | 声明='+(document.querySelector('input[placeholder*=\"创作声明\"]')||{}).value"
