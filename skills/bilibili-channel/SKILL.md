---
name: bilibili-channel
description: 运营个人 B站账号：用官方网页投稿长视频（第三方接口投稿会被风控）、封面/简介/标签/分区/AI 声明、改稿、进度条分段章节、置顶章节评论、系列列表、空间首页（公告/置顶/代表作）、发动态宣传、合规引流（不挂站外视频平台链接）。触发：「传到 B站 / 哔哩哔哩」「B站章节」「B站合集/系列/播放列表」「B站简介怎么写」「B站引流」「发 B站动态」。
---

# B站账号运营

全部走本机 Chrome（browser-cli 的 `ab.local`，已登录账号）里的**官方网页**。

| 做什么 | 走哪条 |
|---|---|
| **投稿视频** | 网页投稿页，`scripts/web_upload.sh` |
| 改标题 / 标签 / 声明 / 换封面 | 网页编辑页（`?type=edit&bvid=`），标题 `set_title.sh`，标签和创作声明 `set_tags_decl.sh`，提交 `submit_click.sh` |
| 进度条分段章节 | `progress_chapters.sh`（播放器 iframe 里的「章节文本编辑器」） |
| 置顶章节评论 | `pin_chapter_comment.sh`（页面内调评论接口 + 置顶接口） |
| 公告 / 置顶 / 代表作 / 系列列表 | `space_setup.sh` |
| 发动态 | 网页 t.bilibili.com（见下） |
| 查稿件状态 | `list_archives.sh`（bvid / 审核中·开放浏览 / 标题）；过审后公开接口 `x/web-interface/view?bvid=` 返回 code 0 |

⛔ **不用第三方接口投稿**（biliup 等）：会被风控报 21566「投稿过于频繁」，client/app/web 三种提交方式全拒，
冷却 18 小时也不解；官方网页/App 不受影响。2026-09 起已从本 skill 移除。

## 投一集（标准流程）

```bash
S=~/.claude/skills/bilibili-channel/scripts
# 1. >1.2GB 先压一版（B站 自己还会转码）：ffmpeg -c:v libx264 -crf 25 -preset faster -tune stillimage -c:a copy
#    带 3 秒封面片头：youtube-channel 的 make_intro.sh，X264="-crf 25 -preset faster" 对齐正片参数
$S/web_upload.sh b01.mp4 cover.jpg "【免费开源课】专题一·…" desc.txt "标签1,标签2,..."   # 纯 ASCII 文件名；默认声明「含AI生成内容」
# 2. 过审后（公开接口 code==0）：
$S/progress_chapters.sh BVxxx chapters.txt        # 每行「m:ss 标题」，≤10 段、标题 ≤~15 字；有片头就整体后移
$S/pin_chapter_comment.sh BVxxx chapters.txt topic-01.html
```

**一次只投一稿**，投完看「稿件投递成功」再投下一个。

封面：和 YouTube 同一套人像版，但加 `COVER_SAFE43=1`（youtube-channel 的 `make_cover_portrait.py`）——
B站 首页推荐按 4:3 裁切，这个模式把题目、人脸、名片全收进中间 4:3。封面弹窗里「双比例同步改动」保持勾选。

## 网页投稿的坑（`web_upload.sh` 已处理）

- 设完视频文件后页面**整段读文件算哈希**，期间任何 CDP 调用都超时，一探测就像「冻死」。先闭嘴等（770 MB 约 200 秒），再轮询「上传完成」。
  真冻死了用 browser-cli 的 `local-chrome.sh` 重启（杀 Chrome 主进程 PID，别 pkill）。
- 只用 `input[type=file]` 设文件；点到会弹系统文件框的区域（「更换视频」、上传封面区）会冻住。封面走「添加封面」弹窗里的 `input[accept^=image]`。
- 新投稿页自带默认标签（生活记录/记录/新人），要逐个删；标签逐个加、中间停 0.8 秒，否则只进第一个。
- 分区会按内容自动选（本课程是「人工智能」），投前看一眼。
- 标题框：上传完成那一刻会被**文件名覆盖**，所以最后设；键盘 Ctrl+A 常选不中，`set_title.sh` 用 JS `select()` 后键入再补发 input 事件，**看字数计数器变了**才算写进表单。
- 「立即投稿」：编辑页只认快照 ref 点击，新投稿页只认滚到可见后的鼠标坐标点击 —— `submit_click.sh` 两种都试。
- 标签框、创作声明下拉不认 ab 的键盘/点击：用 JS 派发 input＋Enter、在关闭图标上派发鼠标事件（`set_tags_decl.sh`）。

## 系列 / 播放列表

- **合集**（视频页右侧显示整套）要权益中心 **Lv2**；够了再升级。
- **系列列表**无门槛：`space_setup.sh series <mid> "列表名" "简介" BV1,BV2,...`，空间「合集和系列」里「播放全部」连播。

## 空间首页

`space_setup.sh notice "…(≤150字)"`、`top BV "理由"`、`masterpiece BV "推荐语"`（最多 3 个）；签名走
`x/member/web/sign/update`。

## 文案与引流（合规）

详见 [references/growth.md](references/growth.md)。硬规矩：

- **简介、评论、动态、视频里都不出现 YouTube**（社区公约 2-9 导流、2-1 VPN 相关，有限流风险）。站外只挂自己的课程网站，由网站做中转。
- 同一内容别重复投稿（3-2）：重传前先让账号主人删旧稿。标签只放相关词；不写「三连必回」类。
- AI 合成配音 → 声明「含AI生成内容」。

## 其他坑

- 脚本拼标题/标签时**别用 `read T G` 拆一行**：标题里有空格，半截标题会跑进标签。
- 删稿要短信验证码＋滑块人机验证：**不要自动破解滑块**，请账号主人在 App 里删。
- 章节提交后要审核，播放器接口 `x/player/v2` 的 `view_points` 过审后才有值。
- 发动态：富文本框要逐行 `insertText` + `insertLineBreak`，整段插入会把第一行挪到最后；点「发布」后还有一次「确认并发送」。
