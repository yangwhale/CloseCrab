#!/bin/bash
# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# 通过 Discord Bot 发送消息到 Discord 频道
# 用法: send-to-discord.sh "报告内容" [标题] [图片路径或URL]
#       send-to-discord.sh --plain "纯文本消息"    (不用 Embed，支持链接预览)
#       send-to-discord.sh --channel <id> "报告内容" [标题]  (指定目标频道)
#       send-to-discord.sh --channel <id> --plain "纯文本"   (指定频道+纯文本)
#       send-to-discord.sh --bot <name> --plain "纯文本"     (指定 bot 身份)
# 或通过 stdin: echo "内容" | send-to-discord.sh "" "标题" [图片]

# 解析前置参数: --bot 和 --channel（顺序无关）
while [[ "$1" =~ ^-- ]]; do
    case "$1" in
        --bot)
            BOT_NAME="$2"
            shift 2
            ;;
        --channel)
            DISCORD_CHANNEL_ID="$2"
            shift 2
            ;;
        --voice)
            VOICE_FILE="$2"
            shift 2
            ;;
        *)
            break
            ;;
    esac
done

# 根据 bot 名称解析 Discord token
# 优先级: 环境变量 DISCORD_BOT_TOKEN > Firestore (via get-bot-secret.py)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_NAME="${BOT_NAME:-${CLOSECRAB_NAME:-jarvis}}"

if [ -z "${DISCORD_BOT_TOKEN:-}" ]; then
    DISCORD_BOT_TOKEN=$(python3 "$SCRIPT_DIR/get-bot-secret.py" "$BOT_NAME" channels.discord.token 2>/dev/null)
fi
BOT_TOKEN="${DISCORD_BOT_TOKEN:?错误: 无法获取 Discord token (bot: ${BOT_NAME})}"
API_BASE="https://discord.com/api/v10"

# 动态获取 DM channel ID（幂等，不会重复创建）
if [ -n "${DISCORD_CHANNEL_ID:-}" ]; then
    CHANNEL_ID="$DISCORD_CHANNEL_ID"
else
    DM_USER_ID="${DISCORD_DM_USER_ID:?Set DISCORD_DM_USER_ID env var}"
    CHANNEL_ID=$(curl -s -X POST \
        -H "Authorization: Bot ${BOT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"recipient_id\": \"${DM_USER_ID}\"}" \
        "${API_BASE}/users/@me/channels" | jq -r '.id')
    if [ -z "$CHANNEL_ID" ] || [ "$CHANNEL_ID" = "null" ]; then
        echo "错误: 无法获取 DM channel ID" >&2; exit 1
    fi
fi

# --voice 模式: 上传 ogg 文件作为语音消息 (Discord Voice Message)
if [ -n "${VOICE_FILE:-}" ]; then
    if [ ! -f "$VOICE_FILE" ]; then
        echo "错误: 语音文件不存在: $VOICE_FILE" >&2; exit 1
    fi
    # Discord Voice Message: flags=8192 + attachment with is_voice_message
    # 需要用 multipart/form-data 上传
    FNAME=$(basename "$VOICE_FILE")
    # 获取音频时长（毫秒），需要 ffprobe
    DURATION_SECS=$(ffprobe -v quiet -show_entries format=duration -of csv=p=0 "$VOICE_FILE" 2>/dev/null || echo "")
    if [ -n "$DURATION_SECS" ]; then
        DURATION_MS=$(echo "$DURATION_SECS * 1000" | bc 2>/dev/null | cut -d. -f1)
    else
        DURATION_MS=""
    fi
    # 构建 payload_json，flags=8192 标记为 voice message
    if [ -n "$DURATION_MS" ]; then
        PAYLOAD_JSON=$(jq -n --arg dur "$DURATION_MS" --arg fname "$FNAME" '{
            "flags": 8192,
            "attachments": [{"id": 0, "filename": $fname, "duration_secs": ($dur | tonumber / 1000), "waveform": ""}]
        }')
    else
        PAYLOAD_JSON=$(jq -n --arg fname "$FNAME" '{
            "flags": 8192,
            "attachments": [{"id": 0, "filename": $fname}]
        }')
    fi
    RESPONSE=$(curl -s -X POST \
        -H "Authorization: Bot ${BOT_TOKEN}" \
        -F "payload_json=${PAYLOAD_JSON}" \
        -F "files[0]=@${VOICE_FILE};type=audio/ogg" \
        "${API_BASE}/channels/${CHANNEL_ID}/messages")
    if echo "$RESPONSE" | grep -q '"id"'; then
        echo "✅ 语音消息已发送到 Discord"
    else
        echo "❌ 语音发送失败: $RESPONSE" >&2; exit 1
    fi
    exit 0
fi

# --plain 模式: 发送纯文本消息（支持 Discord 链接预览）
if [ "$1" = "--plain" ]; then
    shift
    PLAIN_TEXT="$1"
    if [ -z "$PLAIN_TEXT" ]; then PLAIN_TEXT=$(cat); fi
    if [ -z "$PLAIN_TEXT" ]; then echo "错误：没有内容可发送"; exit 1; fi
    PAYLOAD=$(jq -n --arg c "$PLAIN_TEXT" '{"content": $c}')
    RESPONSE=$(curl -s -X POST \
        -H "Authorization: Bot ${BOT_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        "${API_BASE}/channels/${CHANNEL_ID}/messages")
    if echo "$RESPONSE" | grep -q '"id"'; then
        echo "✅ 消息已发送到 Discord"
    else
        echo "❌ 发送失败: $RESPONSE" >&2; exit 1
    fi
    exit 0
fi

CONTENT="$1"
TITLE="${2:-Claude Code 任务报告}"
IMAGE="$3"

# 如果内容为空，从 stdin 读取
if [ -z "$CONTENT" ]; then
    CONTENT=$(cat)
fi

if [ -z "$CONTENT" ]; then
    echo "错误：没有内容可发送"
    exit 1
fi

# Discord embed 描述限制 4096 字符，总消息限制 6000 字符
# 超长时分多条发送
send_message() {
    local payload="$1"
    local file="$2"

    if [ -n "$file" ] && [ -f "$file" ]; then
        # 带本地文件上传
        RESPONSE=$(curl -s -X POST \
            -H "Authorization: Bot ${BOT_TOKEN}" \
            -F "payload_json=${payload}" \
            -F "files[0]=@${file}" \
            "${API_BASE}/channels/${CHANNEL_ID}/messages")
    else
        RESPONSE=$(curl -s -X POST \
            -H "Authorization: Bot ${BOT_TOKEN}" \
            -H "Content-Type: application/json" \
            -d "$payload" \
            "${API_BASE}/channels/${CHANNEL_ID}/messages")
    fi

    # 检查结果
    if echo "$RESPONSE" | grep -q '"id"'; then
        return 0
    else
        ERROR_MSG=$(echo "$RESPONSE" | grep -o '"message":"[^"]*"' | head -1)
        echo "❌ 发送失败: ${ERROR_MSG:-$RESPONSE}" >&2
        return 1
    fi
}

# 构建 embed 消息
build_embed_payload() {
    local title="$1"
    local description="$2"
    local image_url="$3"

    if [ -n "$image_url" ]; then
        # 网络图片通过 embed image 嵌入
        jq -n \
            --arg title "$title" \
            --arg desc "$description" \
            --arg img "$image_url" \
            '{
                "embeds": [{
                    "title": $title,
                    "description": $desc,
                    "color": 5814783,
                    "image": {"url": $img},
                    "footer": {"text": "Claude Code"}
                }]
            }'
    else
        jq -n \
            --arg title "$title" \
            --arg desc "$description" \
            '{
                "embeds": [{
                    "title": $title,
                    "description": $desc,
                    "color": 5814783,
                    "footer": {"text": "Claude Code"}
                }]
            }'
    fi
}

# 判断图片是 URL 还是本地文件
IMAGE_URL=""
IMAGE_FILE=""
if [ -n "$IMAGE" ]; then
    if [[ "$IMAGE" =~ ^https?:// ]]; then
        IMAGE_URL="$IMAGE"
    elif [ -f "$IMAGE" ]; then
        IMAGE_FILE="$IMAGE"
    else
        echo "⚠️  图片不存在: $IMAGE" >&2
    fi
fi

# 分割长内容（embed description 限制 4096 字符）
MAX_LEN=4000
SENT=0

if [ ${#CONTENT} -le $MAX_LEN ]; then
    # 内容不超长，一次发送
    if [ -n "$IMAGE_FILE" ]; then
        # 本地文件：用 attachment:// 引用，保留原始文件名
        FNAME=$(basename "$IMAGE_FILE")
        PAYLOAD=$(jq -n \
            --arg title "$TITLE" \
            --arg desc "$CONTENT" \
            --arg att "attachment://${FNAME}" \
            '{
                "embeds": [{
                    "title": $title,
                    "description": $desc,
                    "color": 5814783,
                    "image": {"url": $att},
                    "footer": {"text": "Claude Code"}
                }]
            }')
        send_message "$PAYLOAD" "$IMAGE_FILE"
    else
        PAYLOAD=$(build_embed_payload "$TITLE" "$CONTENT" "$IMAGE_URL")
        send_message "$PAYLOAD"
    fi
    SENT=$?
else
    # 内容超长，分片发送
    FIRST=true
    SENT=0
    while [ ${#CONTENT} -gt 0 ]; do
        if [ ${#CONTENT} -le $MAX_LEN ]; then
            CHUNK="$CONTENT"
            CONTENT=""
        else
            # 找最近的换行分割
            CHUNK="${CONTENT:0:$MAX_LEN}"
            LAST_NL=$(echo "$CHUNK" | grep -b -o $'\n' | tail -1 | cut -d: -f1)
            if [ -n "$LAST_NL" ] && [ "$LAST_NL" -gt 2000 ]; then
                CHUNK="${CONTENT:0:$LAST_NL}"
                CONTENT="${CONTENT:$LAST_NL}"
            else
                CONTENT="${CONTENT:$MAX_LEN}"
            fi
        fi

        if [ "$FIRST" = true ]; then
            # 第一条带标题和图片
            if [ -n "$IMAGE_FILE" ]; then
                FNAME=$(basename "$IMAGE_FILE")
                PAYLOAD=$(jq -n \
                    --arg title "$TITLE" \
                    --arg desc "$CHUNK" \
                    --arg att "attachment://${FNAME}" \
                    '{
                        "embeds": [{
                            "title": $title,
                            "description": $desc,
                            "color": 5814783,
                            "image": {"url": $att},
                            "footer": {"text": "Claude Code"}
                        }]
                    }')
                send_message "$PAYLOAD" "$IMAGE_FILE" || SENT=1
            else
                PAYLOAD=$(build_embed_payload "$TITLE" "$CHUNK" "$IMAGE_URL")
                send_message "$PAYLOAD" || SENT=1
            fi
            FIRST=false
        else
            # 后续条只有内容
            PAYLOAD=$(build_embed_payload "$TITLE (续)" "$CHUNK" "")
            send_message "$PAYLOAD" || SENT=1
        fi

        [ ${#CONTENT} -gt 0 ] && sleep 0.5
    done
fi

if [ $SENT -eq 0 ]; then
    echo "✅ 报告已发送到 Discord"
else
    exit 1
fi