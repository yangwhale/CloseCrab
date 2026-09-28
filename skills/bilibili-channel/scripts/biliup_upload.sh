#!/usr/bin/env bash
# 用 biliup-rs 投稿（首选路径：接口直传，几十 MB/s，不经过网页上传器）
# 首次：biliup login 选「扫码登录」，用 B站 App 扫 qrcode.png → 生成 cookies_tv.json（等同账号密码，600 权限，别进 git）
# 用法：biliup_upload.sh 视频.mp4 封面.jpg "标题" 简介.txt "标签1,标签2,..." [分区tid，默认 231]
# ⛔ 文件名用纯 ASCII；>1.2GB 先压一版（crf 25，B站 自己还会再转码）
set -euo pipefail
BIN=${BILIUP_BIN:-$HOME/lecvid/biliup/biliupR-v0.2.4-x86_64-linux/biliup}; CK=${BILIUP_COOKIE:-$HOME/lecvid/biliup/cookies_tv.json}
"$BIN" -u "$CK" upload --tid "${6:-231}" --cover "$2" --title "$3" --desc "$(cat "$4")" --tag "$5" "$1" 2>&1 | grep -E "Upload completed|投稿成功|bvid|Error" | sed 's/.*"bvid": String("\([^"]*\)").*/bvid=\1/'
