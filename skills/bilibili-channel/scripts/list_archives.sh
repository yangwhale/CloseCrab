#!/usr/bin/env bash
# 列出自己的稿件（审核中 / 已通过 / 未通过）：bvid、状态、标题。在已登录的本机 Chrome 里调创作中心接口
A="timeout 40 $HOME/.claude/skills/browser-cli/scripts/ab.local"
$A open "https://member.bilibili.com/platform/upload-manager/article" >/dev/null; sleep 4
$A eval "(async()=>{const r=await (await fetch('https://member.bilibili.com/x/web/archives?status=is_pubing%2Cpubed%2Cnot_pubed&pn=1&ps=50',{credentials:'include'})).json();return (r.data.arc_audits||[]).map(a=>a.Archive.bvid+'\t'+a.Archive.state_desc+'\t'+a.Archive.title).join('\n')})()" | python3 -c "import json,sys;print(json.loads(sys.stdin.read()))"
