---
name: lecture-video
description: 把「一节课的讲课录音 ＋ 课件网页」做成讲课视频：讲到哪句，页面平滑滚到对应的图并画框/圈/下划线，页面里的动画按时间同步播放，带句级字幕。一节一节做。触发：「生成课程视频」「把这一节做成视频」「给录音配画面」「讲课视频」「lecture video」「做第 N 节视频」。录音是主资产，视频只配画面，不改音频。
---

# 讲课视频（录音 ＋ 课件 → mp4）

**原则：录音是主资产，一个字都不动。** 视频只做一件事——讲到哪，课件翻到哪、框到哪。

## 流程（一节）

```bash
SK=~/.claude/skills/lecture-video/scripts
# 0. 录音：由 lecture-voice skill 产出（一节一节讲、认可后存现场原声）；这里默认它已经在 media/ 里
# 1. 逐字时间戳（faster-whisper large-v3，CPU 约 1× 实时，8 分钟录音约 15 分钟 —— 后台跑）
setsid nohup ~/.venvs/fw/bin/python $SK/align.py s<N>.wav s<N>-words.json > align.log 2>&1 &
# 2. 句级字幕：文字用原稿（术语准），时间用识别结果
python3 $SK/srt.py s<N>.txt s<N>-words.json s<N>.srt
# 3. 看页面结构、给每张图截 PNG，量子区域
python3 $SK/inspect_page.py 页面.html '#s<N>的id' /tmp/lv [--open 'details:has(#某动画)']
# 4. 写提示表 s<N>-cues.json（格式见 references/cues.md），at 用台词开头几个字
python3 $SK/resolve.py s<N>-cues.json s<N>-words.json s<N>-timed.json   # at → 秒；模糊命中会打印出来，核一眼
# 5. 渲染（12 片并行，8 分钟一节约 2 分半）；先 --until 40 出个短片看构图
python3 $SK/render.py s<N>-timed.json out.mp4 --shards 12
# 6. 加字幕、压体积 → 进课件
$SK/finalize.sh out.mp4 s<N>.srt media/<课>-video-s<N>.mp4
```

环境：`python3 -m venv ~/.venvs/fw && ~/.venvs/fw/bin/pip install faster-whisper`；系统 python 要有 `playwright`（chromium）和 ffmpeg。

## 验收（出片后必做）

抽 8–10 个提示时间点截帧拼成一张图看：框是不是框在正在讲的那块、滚动有没有把目标切掉、动画有没有在放。
**时机错** → 改 `at`；**位置错** → 改 `sub`；**看不全** → 目标太高时会自动改看子区域，必要时拆成两条提示。

## 放进课件

每节开头一个「录音 ＋ 视频」各占一半的条（专题五：`topic03_page.lecture_media`），文件名约定
`media/<课>-lecture-s<N>.mp3`、`media/<课>-video-s<N>.mp4`，文件在就显示、不在就只显示录音。
带时间轴的文字稿（`s<N>-words.json`、`s<N>.srt`）跟录音放在一起进仓库，以后对内容、对口型都以它为准。

## 坑

- whisper 会把术语听成同音字（归约→规约、请看→数凭），resolve 用模糊匹配兜住；**at 别挑太短、太常见的词**。
- 页面自带的分步播放器会自己暂停：render 把 video 全换成没挂监听的新节点、逐帧设 `currentTime`，**不要**让页面自己播。
- 折叠在 `<details>` 里的动画要在 `open` 里点名打开，否则量不到位置。
- 课件上的录音/视频条渲染时会藏掉（`.lecaudio,.lecmedia`），免得画中画。
- 视频要进 git：用 finalize 压（crf 28 + stillimage，8 分钟约 20 MB），别直接提交渲染原片（40 MB+）。
