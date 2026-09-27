#!/usr/bin/env bash
# 不经过语音通道，批量把讲稿合成为讲课录音（与现场语音同一条合成路径：offline_tts.py）。
# 用法：synth_all.sh <讲稿目录> <media目录> <课前缀> s1 s2 ...   例：synth_all.sh tools/topic01-audio WebPages/media topic01 s1 s2
set -uo pipefail
SRC="$1"; MEDIA="$2"; PFX="$3"; shift 3
D=$(dirname "$(readlink -f "$0")")
for s in "$@"; do
  ( BOT_NAME=${BOT_NAME:-jarvis} python3 "$D/offline_tts.py" "$SRC/$s.txt" "/tmp/$PFX-$s.pcm" 2>&1 | grep -v -i warn | tail -1 \
    && ffmpeg -y -loglevel error -f s16le -ar 48000 -ac 2 -i "/tmp/$PFX-$s.pcm" -ac 1 -b:a 64k "$MEDIA/$PFX-lecture-$s.mp3" \
    && echo "$s → $MEDIA/$PFX-lecture-$s.mp3 $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$MEDIA/$PFX-lecture-$s.mp3")s" ) &
done
wait
