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
# 1. 逐字时间戳（faster-whisper large-v3）。⭐ 有 GPU 就用 GPU：八节一起、一卡一节，90 秒全部跑完；
#    CPU 只有 0.3× 实时左右（8 分钟录音要二十多分钟）。GPU 版把 WhisperModel 改成 device="cuda", compute_type="float16"，
#    并把 venv 里 nvidia/cublas、nvidia/cudnn 的 lib 目录加进 LD_LIBRARY_PATH
setsid nohup ~/.venvs/fw/bin/python $SK/align.py s<N>.wav s<N>-words.json > align.log 2>&1 &
# 2. 句级字幕：文字用原稿（术语准），时间用识别结果
python3 $SK/srt.py s<N>.txt s<N>-words.json s<N>.srt
# 3. 看页面结构、给每张图截 PNG，量子区域
python3 $SK/inspect_page.py 页面.html '#s<N>的id' /tmp/lv [--open 'details:has(#某动画)']
# 4. 写提示表 s<N>-cues.json（格式见 references/cues.md），at 用台词开头几个字
python3 $SK/resolve.py s<N>-cues.json s<N>-words.json s<N>.txt s<N>-timed.json   # at 必须照抄文字稿；时间来自全局对齐
# 5. 渲染（12 片并行，8 分钟一节约 2 分半）；先 --until 40 出个短片看构图
python3 $SK/render.py s<N>-timed.json out.mp4 --shards 12
# 6. 加字幕、压体积 → 进课件
$SK/finalize.sh out.mp4 s<N>.srt media/<课>-video-s<N>.mp4
```

环境：`python3 -m venv ~/.venvs/fw && ~/.venvs/fw/bin/pip install faster-whisper`；系统 python 要有 `playwright`（chromium）和 ffmpeg。

## 提示表先过浏览器再渲染

提示表的 `target` 必须在**真浏览器**里逐条 `querySelector` 选得中（用和 `render.py` 同一种选法），**并配一个反例**：
故意改坏一条，检查必须报错、退出码非 0。只查「id 在 html 字符串里存在」不够 ——
课件站点常把表格包在 `<div class="tbl">` 里，`标题 ~ table` 字符串里照样过、浏览器里选不中，要到 `render.py` 才炸（应写 `标题 ~ div.tbl`）。

## 验收（出片后必做）

抽 8–10 个提示时间点截帧拼成一张图看：框是不是框在正在讲的那块、滚动有没有把目标切掉、动画有没有在放。
**时机错** → 改 `at`；**位置错** → 改 `sub`；**看不全** → 目标太高时会自动改看子区域，必要时拆成两条提示。

## 放进课件

每节开头一个「录音 ＋ 视频」各占一半的条（专题五：`topic03_page.lecture_media`），文件名约定
`media/<课>-lecture-s<N>.mp3`、`media/<课>-video-s<N>.mp4`，文件在就显示、不在就只显示录音。
带时间轴的文字稿（`s<N>-words.json`、`s<N>.srt`）跟录音放在一起进仓库，以后对内容、对口型都以它为准。

## 坑

- **就地渲染成品页面，不要拷到别处**：样式表、图片是相对路径，拷到 `/tmp` 画面就没样式。
  多个 agent 同时在重建同一个输出目录时，等构建完再渲；检查只拿构建完的快照。
- **输出路径**：`render.py` 2026-09-29 起自己转绝对路径；更老的副本传相对路径会让分片目录拼两遍、ffmpeg 拼接失败，要手动写绝对路径。
- **页面图片 `loading="lazy"`**：没滚到的图尺寸为 0，截图会超时或截出空框。先做一份去掉 lazy 的临时副本（放在原目录、用完即删，别拷走），或逐段滚动等加载完。
- **本地截图用 Playwright 自带的 chromium**：沙箱里系统 headless chrome 可能起不来；`browser-cli` 连的是远端浏览器，读不到本机 `file://`。
- **互动的答案别提前上屏**：自动提示只会「讲到哪框到哪」，做不出先遮后揭；揭晓用 `addclass` 手写时刻，或揭晓前切到不含答案的一屏。

- whisper 会把术语听成同音字（归约→规约）、把中文数字写成阿拉伯数字（百分之九十七→97%）。逐句找开头会在这些地方跑偏、一偏后面全挤到结尾 —— 所以 srt/resolve 都走 `tmap.py` 的**整篇全局对齐**，对不上的段落两头插值。
- **TTS 偶尔会漏念整句**（第零节就漏了三句）：srt.py 会打印「录音里像是没念」，这些句子不出字幕；要补就重录那一节。
- 页面自带的分步播放器会自己暂停：render 把 video 全换成没挂监听的新节点、逐帧设 `currentTime`，**不要**让页面自己播。
- 折叠在 `<details>` 里的动画要在 `open` 里点名打开，否则量不到位置。
- 课件上的录音/视频条渲染时会藏掉（`.lecaudio,.lecmedia`），免得画中画。
- 视频要进 git：用 finalize 压（crf 28 + stillimage，8 分钟约 20 MB），别直接提交渲染原片（40 MB+）。
- **画面要像专题五那样铺满**：正文栏窄、没有通栏大图的页面，2200 视口下两侧大片留白。加 `--vw 1500`～`1600`
  把 CSS 视口调窄，内容放大铺满（像素密度自动换算，4K 清晰度不变）；提示表不用改（按选择器和相对子区域定位）。
- **拼整片前统一音频**：补录过的段是 24 kHz 单声道，直接 concat 会让那几段声音坏掉而不报错 ——
  先把每段转成 48 kHz 双声道再拼（`make_full.py` 已内置），拼完抽几处解码 + STT 验一下。
- **要传 YouTube / B 站，单独出一版 4K，别拿网页版凑合**：`render.py … --scale 2` ＋ `finalize.sh --hd`。
  `--scale 2` 是把设备像素比设成 2：版面不变，字和线条按两倍像素重画（不是放大），出 3840×2160。
  网页版糊有两个原因：一是 1080p 本身把小字压到只有几个像素高，二是 crf 28 把灰色小字压出了块状噪点。
  而平台会按上传分辨率分配码率，传 1080p 二压后更糊。4K 成片不进 git，字幕 .srt 另外单独上传，
  平台上观众就能开关字幕。
