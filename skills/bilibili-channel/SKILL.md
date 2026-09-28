---
name: bilibili-channel
description: 运营个人 B站账号：用 biliup-rs 接口投稿长视频（网页上传器会冻住页面）、封面/简介/标签/分区/AI 声明、进度条分段章节、置顶章节评论、系列列表、空间首页（公告/置顶/代表作）、发动态宣传、合规引流（不挂站外视频平台链接）。触发：「传到 B站 / 哔哩哔哩」「B站章节」「B站合集/系列/播放列表」「B站简介怎么写」「B站引流」「发 B站动态」。
---

# B站账号运营

## 通道选择（踩出来的结论）

| 做什么 | 走哪条 |
|---|---|
| **投稿视频** | **biliup-rs**（`scripts/biliup_upload.sh`）。接口直传几十 MB/s，秒级提交 |
| 改标题 / 声明 / 换封面 | 网页编辑页（`?type=edit&bvid=`），标题用 `set_title.sh`，提交用 `submit_click.sh` |
| 进度条分段章节 | `progress_chapters.sh`（播放器 iframe 里的「章节文本编辑器」） |
| 置顶章节评论 | `pin_chapter_comment.sh`（评论接口 + 置顶接口） |
| 公告 / 置顶 / 代表作 / 系列列表 | `space_setup.sh` |
| 发动态 | 网页 t.bilibili.com（见下） |

⛔ **别用网页上传器传新视频**：新文件一传页面就冻死（CDP 全部超时，只能杀 Chrome 重启）；
点到会弹系统文件框的区域（「更换视频」、上传封面区）也会冻住。只用 `input[type=file]` 设文件，从不点上传区。

## 前置（一次性）

- biliup-rs：`gh release download -R biliup/biliup-rs -p '*x86_64-linux.tar.xz'`；`biliup login` 选「扫码登录」，
  把 `qrcode.png` 发给账号主人用 B站 App 扫 → `cookies_tv.json`（等同账号权限，600，别进 git）。
  「网页 Cookie 登录」两种都已失效（502/未登录），别再试。
- 本机 Chrome（browser-cli `ab.local`）登录同一账号，给网页编辑、章节、评论、空间设置用。

## 投一集（标准流程）

```bash
S=~/.claude/skills/bilibili-channel/scripts
# 1. >1.2GB 先压一版（B站 自己还会转码）：ffmpeg -c:v libx264 -crf 25 -preset faster -tune stillimage -c:a copy
$S/biliup_upload.sh b01.mp4 cover.jpg "【免费开源课】专题一·…" desc.txt "标签1,标签2,..."   # 纯 ASCII 文件名
# 2. 过审后（公开接口 code==0）：
#    编辑页把「创作声明」设为「含AI生成内容」（配音是 AI 合成就必须标）
$S/progress_chapters.sh BVxxx chapters.txt        # 每行「m:ss 标题」，≤10 段、标题 ≤~15 字
$S/pin_chapter_comment.sh BVxxx chapters.txt topic-01.html
```

封面：16:9 设计、内容放在中间 4:3 安全区（B站 首页推荐用 4:3 裁切），用 youtube-channel 的 `make_cover.py` 即可。
封面弹窗里「双比例同步改动」要勾上，否则只换了一个比例。

## 系列 / 播放列表

- **合集**（视频页右侧显示整套）要权益中心 **Lv2**；够了再升级。
- **系列列表**无门槛：`space_setup.sh series <mid> "列表名" "简介" BV1,BV2,...`，空间「合集和系列」里「播放全部」连播。

## 空间首页

`space_setup.sh notice "…(≤150字)"`、`top BV "理由"`、`masterpiece BV "推荐语"`（最多 3 个）；签名走
`x/member/web/sign/update`。

## 文案与引流（合规）

详见 [references/growth.md](references/growth.md)。硬规矩：

- **简介、评论、动态、视频里都不出现 YouTube**（社区公约 2-9 导流、2-1 VPN 相关，有限流风险）。站外只挂自己的课程网站，由网站做中转。
- 同一内容别重复投稿（3-2）；标签只放相关词；不写「三连必回」类。
- AI 合成配音 → 声明「含AI生成内容」。

## 坑

- 标题框：上传完成那一刻会被**文件名覆盖**；改标题必须 click→Ctrl+A→Delete→type→Tab，**看字数计数器变了**才写进了表单模型，直接改 value 无效。
- 提交按钮用鼠标坐标点（`submit_click.sh`），DOM `.click()` 偶尔不生效。
- 删稿要短信验证码＋滑块人机验证：**不要自动破解滑块**，请账号主人在 App 里删。
- 章节提交后要审核，播放器接口 `x/player/v2` 的 `view_points` 过审后才有值。
- 发动态：富文本框要逐行 `insertText` + `insertLineBreak`，整段插入会把第一行挪到最后；点「发布」后还有一次「确认并发送」。
