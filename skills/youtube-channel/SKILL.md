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
python3 $S/make_cover.py cover.png "专题一" "一个 Token 的一生" "副标题" "要点1|要点2|要点3"   # 16:9，4:3 安全区，标题自动缩放
URL=$($S/studio_upload.sh full-4k.mp4 "标题" desc.txt)       # 公开发布，打印 youtu.be 链接
python3 $S/post_upload.py <videoId> full.srt cover.jpg 0 "标签1,标签2"   # 字幕 + 标签 + 封面 + 进播放列表
```

长课整片：`make_full.py <段目录> <前缀> <字幕目录> "s0=标题|s1=标题|..."` 把各段 4K 拼起来，
**先把每段音频统一成 48 kHz 双声道**（补录过的段是 24 kHz，直接 concat 声音会坏且不报错），同时生成合并字幕和 `00:00` 章节表。

## 写标题 / 简介 / 章节（能被搜到的写法）

详见 [references/seo.md](references/seo.md)，要点：

- **标题**：`【系列名】第N讲 核心问题：关键词A、关键词B`，英文术语原样保留，≤100 字符。
- **简介首行**放 1–2 个主关键词，接着是课件链接，再往下是完整 `00:00` 章节（≥3 段、每段 ≥10 秒）。
- **英文本地化**（官方：翻译过的标题和描述会被英文搜索索引）：每个视频和播放列表都写 `localizations.en`，标题同样 ≤100 字符（别硬截断）。
- **tags 作用很小**（官方原话），填几个即可；#话题标签放 3–5 个。
- 播放列表标题也写搜索词；视频按讲序排好，删掉「Deleted video」残项。

## Shorts 引流

`make_short.py 全片.mp4 全片.srt out.mp4 "起-止,起-止" "标题行1|标题行2" "结尾引导"`：
横屏课件裁掉两侧留白放大、顶部两行标题、底部大字幕、结尾 CTA，60–120 秒。
挑「一个问题 + 一个反直觉的数」的片段（例：「训练 DeepSeek-V3 要几张卡？一个参数 16 字节」）。
Shorts 描述里链接不可点，在 Studio 里设「Related video」指向完整版。

## 坑

- 原文件直接交给 Studio 偶发「File unreadable」，**拷一份新文件再传**就好（脚本已内置）。
- Shorts 的链接形如 `youtube.com/shorts/ID`，不是 `youtu.be/ID`。
- Studio 上传框里 kids 单选、公开单选都要用 `tp-yt-paper-radio-button[name=...]` 点，`#next-button` 连点三次到可见性页。
- 播放列表 `position` 超过现有条数会 400，先插末尾再统一 update 排序。
