#!/usr/bin/env bash
# 片头：封面静帧 3 秒 + 轻提示音。参数必须和正片一致（x264 crf18 medium、25fps、timescale 12800、48k AAC、声道数跟正片），
# 这样才能 concat -c copy 直接接在正片前面，不用重编两小时的 4K。
# 正片若是另一套编码参数（如 B站 小体积版 crf25 faster），用 X264="-crf 25 -preset faster" 对齐
# 用法：make_intro.sh cover-4k.png 正片.mp4 out.mp4 [秒数]
set -euo pipefail
cover=$1 main=$2 out=$3 sec=${4:-3}
ch=$(ffprobe -v error -select_streams a:0 -show_entries stream=channels -of csv=p=0 "$main")
ab=$(ffprobe -v error -select_streams a:0 -show_entries stream=bit_rate -of csv=p=0 "$main"); ab=$(( (ab+500)/1000 ))k
# 两个音（C6→G6）指数衰减，-20 dB 左右，最后 0.5 秒前衰完
chime="0.18*exp(-6*t)*sin(2*PI*1046.5*t)+0.15*between(t,0.14,9)*exp(-5*(t-0.14))*sin(2*PI*1568*(t-0.14))"
ffmpeg -v error -y -loop 1 -framerate 25 -i "$cover" -f lavfi -i "aevalsrc=exprs='${chime}':s=48000:d=${sec}" \
  -t "$sec" -vf "scale=3840:2160,format=yuv420p" -c:v libx264 ${X264:--crf 18 -preset medium} -tune stillimage -r 25 \
  -c:a aac -ar 48000 -ac "$ch" -b:a "$ab" -video_track_timescale 12800 -shortest "$out.intro.mp4"
printf "file '%s'\nfile '%s'\n" "$(realpath "$out.intro.mp4")" "$(realpath "$main")" > "$out.cat.txt"
ffmpeg -v error -y -f concat -safe 0 -i "$out.cat.txt" -map 0:v -map 0:a -c copy -movflags +faststart "$out"
rm -f "$out.cat.txt"
echo "$out $(ffprobe -v error -show_entries format=duration -of csv=p=0 "$out")"
