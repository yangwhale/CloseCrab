#!/bin/bash
# board-push.sh — 更新白板内容（写 JSON 到 GCS，页面自动拉取）
# 用法:
#   board-push.sh add '<h2>标题</h2><p>内容</p>'    # 添加一步
#   board-push.sh clear                              # 清空
#   board-push.sh title '主题名'                     # 设标题
#   board-push.sh highlight 2                        # 高亮第 N 步

STATE_FILE="/gcs/cc-pages/pages/board-state.json"

# 初始化状态文件（如果不存在）
if [ ! -f "$STATE_FILE" ]; then
    echo '{"title":"Jarvis Whiteboard","subtitle":"","steps":[],"highlight":-1,"ts":0}' > "$STATE_FILE"
fi

case "$1" in
    add)
        python3 -c "
import json, sys, time
with open('$STATE_FILE') as f: s = json.load(f)
s['steps'].append(sys.argv[1])
s['highlight'] = len(s['steps']) - 1
s['ts'] = int(time.time()*1000)
with open('$STATE_FILE', 'w') as f: json.dump(s, f, ensure_ascii=False)
print(f'Step {len(s[\"steps\"])-1} added')
" "$2"
        ;;
    clear)
        python3 -c "
import json, time
s = {'title':'Jarvis Whiteboard','subtitle':'','steps':[],'highlight':-1,'ts':int(time.time()*1000)}
with open('$STATE_FILE', 'w') as f: json.dump(s, f, ensure_ascii=False)
print('Cleared')
"
        ;;
    title)
        python3 -c "
import json, sys, time
with open('$STATE_FILE') as f: s = json.load(f)
s['title'] = sys.argv[1]
if len(sys.argv) > 2: s['subtitle'] = sys.argv[2]
s['ts'] = int(time.time()*1000)
with open('$STATE_FILE', 'w') as f: json.dump(s, f, ensure_ascii=False)
print(f'Title set: {sys.argv[1]}')
" "$2" "${3:-}"
        ;;
    highlight)
        python3 -c "
import json, sys, time
with open('$STATE_FILE') as f: s = json.load(f)
s['highlight'] = int(sys.argv[1])
s['ts'] = int(time.time()*1000)
with open('$STATE_FILE', 'w') as f: json.dump(s, f, ensure_ascii=False)
print(f'Highlight step {sys.argv[1]}')
" "$2"
        ;;
    *)
        echo "Usage: board-push.sh {add|clear|title|highlight} [args]"
        ;;
esac
