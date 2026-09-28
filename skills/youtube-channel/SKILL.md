---
name: youtube-channel
description: 运营个人 YouTube 频道：上传长视频（网页 Studio 自动化，绕开未审核 API 的私享锁）、用 Data API 补字幕/封面/标签/播放列表/英文本地化、写能被搜到的标题简介章节、做竖屏 Shorts 引流、删改视频。触发：「传到 YouTube」「YouTube 简介怎么写」「YouTube 引流/SEO」「更新 YouTube 播放列表」「做 Shorts」「YouTube 封面」。
---

# YouTube 频道运营

两条通道，各干各的：

| 做什么 | 走哪条 | 为什么 |
|---|---|---|
| **上传视频本身** | 网页 Studio 自动化（`scripts/studio_upload.sh`） | 未过 API 审核的项目用 `videos.insert` 传的视频**被强制锁成私享**，Studio 网页传不受限 |
| 字幕、标签、封面、播放列表、改标题简介、英文本地化、删视频 | Data API（`scripts/post_upload.py` 等） | 私享锁只管 `videos.insert`，其余接口正常 |

## 前置（一次性）

- 本机 Chrome（browser-cli 的 `ab.local`）登录频道账号。
- GCP 项目启用 YouTube Data API v3，OAuth 同意屏幕「外部」、把频道账号加为测试用户，建「桌面应用」客户端 → `~/.config/youtube/client_secret.json`。
- `python3 scripts/oauth_login.py`，在已登录的 Chrome 打开打印出的 URL 点同意 → `token.json`。Testing 状态下 **7 天过期**，过期重跑。
- 超过 15 分钟的视频要先手机验证（youtube.com/verify）；简介里可点击的外链、自定义封面属于中/高级功能，按提示验证。
- 长期想让 API 直传公开：填 YouTube API Services Audit 表（要公开的隐私政策、服务条款、证据截图），周期不定。

## 上传一集（标准流程）

```bash
export YT_CHANNEL_ID=UC...  YT_PLAYLIST=PL...
S=~/.claude/skills/youtube-channel/scripts
python3 $S/make_cover_portrait.py cover.png "专题一" "一个 Token|的一生" "钩子1|钩子2"   # 标准人像封面（见下节）；无人像时退回 make_cover.py 纯文字版
$S/make_intro.sh cover-4k.png full-4k.mp4 full-4k-intro.mp4 3                           # 片头，上传的是带片头这份
URL=$($S/studio_upload.sh full-4k.mp4 "标题" desc.txt)       # 公开发布，打印 youtu.be 链接
python3 $S/post_upload.py <videoId> full.srt cover.jpg 0 "标签1,标签2"   # 字幕 + 标签 + 封面 + 进播放列表
```

## 封面与片头（频道统一风格）

**标准封面＝人像版**：左边系列名、「免费开源课」红标、专题号、大字题目、两行钩子（黄竖线）；右边蓝圆盘上的作者半身像，右下角白名片「作者 名字 / 头衔 / 一句话专长」。

```bash
export COVER_PORTRAIT=portrait.png COVER_NAME="名字" COVER_ROLE="头衔" COVER_BIO="一句话专长"
python3 $S/make_cover_portrait.py b05.png "专题五" "并行策略" "钩子第一行|钩子第二行"      # 标题里 | 换行
COVER_SCALE=2 python3 $S/make_cover_portrait.py c05.png ...                               # 4K 版给片头用
$S/make_intro.sh c05.png full-4k.mp4 out.mp4 3     # 封面静帧 3 秒＋轻提示音，接在正片前
```

- 人像：从作者给的照片/海报抠（`rembg`），腰部以上，底部 140px 渐隐；**海报上的大会 logo、主办方标识、二维码不用**（别人的商标）。
  人像文件和作者信息不进仓库，放本机品牌目录。
- **封面 ≠ 片头**：缩略图只在点开前显示（搜索、推荐、外站嵌入），平台不会插到视频开头。要一点播放就看到，得把封面剪进视频做片头 ——
  **上传前就要带上**，已发布的视频不能在开头加东西（Studio 编辑器只能剪/打码/配乐），补片头＝重传。
- `make_intro.sh` 按正片参数编片头（x264 crf18 medium、25fps、timescale 12800、48k AAC、声道数跟正片），再 `concat -c copy`，两小时 4K 秒级拼完不重编。
  加了片头，**章节（首个 0:00 不动）和字幕都要后移同样秒数**。

长课整片：`make_full.py <段目录> <前缀> <字幕目录> "s0=标题|s1=标题|..."` 把各段 4K 拼起来，
**先把每段音频统一成 48 kHz 双声道**（补录过的段是 24 kHz，直接 concat 声音会坏且不报错），同时生成合并字幕和 `00:00` 章节表。

## 写标题 / 简介 / 章节（能被搜到的写法）

详见 [references/seo.md](references/seo.md)，要点：

- **标题**：`【系列名】第N讲 核心问题：关键词A、关键词B`，英文术语原样保留，≤100 字符。
- **简介首行**放 1–2 个主关键词，接着是课件链接，再往下是完整 `00:00` 章节（≥3 段、每段 ≥10 秒）。
- **英文本地化**（官方：翻译过的标题和描述会被英文搜索索引）：每个视频和播放列表都写 `localizations.en`，标题同样 ≤100 字符（别硬截断）。
- **tags 作用很小**（官方原话），填几个即可；#话题标签放 3–5 个。
- 播放列表标题也写搜索词；视频按讲序排好，删掉「Deleted video」残项。

## 高级功能开通后（视频验证通过）必做

- **频道**：`channels.update(part=brandingSettings)` 写简介/关键词/`unsubscribedTrailer`（未订阅访客的预告片＝第 1 讲）；
  `localizations` 必须**单独一次 update**（和 brandingSettings 同发会 400）。`channelSections.insert` 加「单个播放列表」＋「最新上传」板块。
- **置顶评论**：`commentThreads.insert` 发（课件链接＋整套播放列表＋下一讲），再 `scripts/pin_comment.sh <视频ID> <评论ID>` 在观看页置顶（API 不能置顶）。
- **片尾画面**：`scripts/end_screen.sh <视频ID> "第N+1讲"` —— 模板「1 视频＋1 播放列表＋订阅」，视频指向下一讲，最后一讲留 Best for viewer。
  坐标按 2200 宽本机 Chrome 标定；已有片尾的视频会跳过模板页（脚本报 still open，点 Discard 关掉即可）。
- **Shorts**：Studio 详情页「Related video」选完整版，存盘。
- 之后：Test & compare 做 2–3 版封面、抽查自动英文配音。

## Shorts 引流

`make_short.py 全片.mp4 全片.srt out.mp4 "起-止,起-止" "标题行1|标题行2" "结尾引导"`：
横屏课件裁掉两侧留白放大、顶部两行标题、底部大字幕、结尾 CTA，60–120 秒。
挑「一个问题 + 一个反直觉的数」的片段（例：「训练 DeepSeek-V3 要几张卡？一个参数 16 字节」）。
Shorts 描述里链接不可点，在 Studio 里设「Related video」指向完整版。

## 重传（补片头 / 换视频）的正确顺序

已发布的视频不能换文件，只能重传。按这个顺序一次做对：

1. **先用 API 把旧视频的元数据存下来**（标题、简介、标签、英文本地化、置顶评论原文）——删了就拿不回来。
2. 传新视频 → `post_upload.py` 补字幕/封面/播放列表（有片头就把字幕、章节后移）。
3. **等新视频处理完、变成公开**再做下一步：Studio 选了「公开」，处理期间 API 看到的仍是 `private`，
   这时发评论会 403（insufficient permissions），不是 token 问题。2 小时 4K 要处理较久，用 watch-task 盯 `privacyStatus`。
4. **删旧视频要在做片尾之前**：`end_screen.sh` 按「第N讲」关键字搜下一讲，新旧同名时会挂到旧的上。
5. 然后：置顶评论（旧 ID 全部换成新 ID）→ 片尾 → 频道预告片 → 播放列表去掉残项并排序 → 外部网站链接。

## 坑

- 原文件直接交给 Studio 偶发「File unreadable」，**拷一份新文件再传**就好（脚本已内置）。
- Shorts 的链接形如 `youtube.com/shorts/ID`，不是 `youtu.be/ID`。
- Studio 上传框里 kids 单选、公开单选都要用 `tp-yt-paper-radio-button[name=...]` 点，`#next-button` 连点三次到可见性页。
- 播放列表 `position` 超过现有条数会 400，先插末尾再统一 update 排序。
