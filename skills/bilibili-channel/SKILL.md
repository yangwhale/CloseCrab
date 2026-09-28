---
name: bilibili-channel
description: 运营个人 B站账号：用 biliup-rs 接口投稿长视频（网页上传器会冻住页面）、封面/简介/标签/分区/AI 声明、进度条分段章节、置顶章节评论、系列列表、空间首页（公告/置顶/代表作）、发动态宣传、合规引流（不挂站外视频平台链接）。触发：「传到 B站 / 哔哩哔哩」「B站章节」「B站合集/系列/播放列表」「B站简介怎么写」「B站引流」「发 B站动态」。
---

# B站账号运营

## 通道选择（踩出来的结论）

| 做什么 | 走哪条 |
|---|---|
| **投稿视频** | **biliup-rs**（`scripts/biliup_upload.sh`）。接口直传几十 MB/s，秒级提交。**被风控（21566）后只能走网页投稿**，见下 |
| 改标题 / 标签 / 声明 / 换封面 | 网页编辑页（`?type=edit&bvid=`），标题 `set_title.sh`，标签和创作声明 `set_tags_decl.sh`，提交 `submit_click.sh` |
| 进度条分段章节 | `progress_chapters.sh`（播放器 iframe 里的「章节文本编辑器」） |
| 置顶章节评论 | `pin_chapter_comment.sh`（评论接口 + 置顶接口） |
| 公告 / 置顶 / 代表作 / 系列列表 | `space_setup.sh` |
| 发动态 | 网页 t.bilibili.com（见下） |

⚠️ **网页投稿（21566 后的唯一出路）**：`input[type=file]` 设完视频后，页面要整段读文件算哈希，**这期间任何 CDP 调用都会超时**，
一探测就像「冻死」。正确做法是设完文件**闭嘴等 ~200 秒**（770 MB 实测）再看「上传完成」；真冻死了用 browser-cli 的 `local-chrome.sh` 重启（杀主进程 PID，别 pkill）。
点到会弹系统文件框的区域（「更换视频」、上传封面区）会冻住。封面走「添加封面」弹窗里的 `input[accept^=image]`。
新投稿页默认标签是「生活记录/记录/新人」，要删掉；标签逐个加、每个之间 sleep 0.8，否则只进第一个。

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

封面：和 YouTube 同一套人像版（youtube-channel 的 `make_cover_portrait.py`，风格说明见那边「封面与片头」）；16:9 设计，题目和人脸都在中间 4:3 安全区内（B站 首页推荐用 4:3 裁切）。
投稿文件用带 3 秒封面片头的那份（`make_intro.sh`），章节时间同样后移。
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
- 提交按钮用快照 ref 点（`submit_click.sh`）；坐标点击在编辑页会静默不生效，DOM `.click()` 也不稳。
- 标签框、创作声明下拉不认 ab 的键盘/点击：用 JS 派发 input＋Enter 键事件、在关闭图标上派发鼠标事件（`set_tags_decl.sh`）。
- **21566「投稿过于频繁」＝第三方接口投稿被风控**，不是频率：client/app/web 三种 `--submit` 全拒，隔 10 分钟重试 3 次、GitHub 上有人冷却 18 小时都没用；官方网页/App 投稿不受影响 → 改网页投稿。
- 脚本拼标题/标签时**别用 `read T G` 拆一行**：标题里有空格，半截标题会跑进标签（踩过：标题被截、多出一个「KV cache 账较劲 人工智能」标签）。
- biliup 投的稿默认声明是「内容为自制」，AI 配音要在编辑页改成「含AI生成内容」。
- 删稿要短信验证码＋滑块人机验证：**不要自动破解滑块**，请账号主人在 App 里删。
- 章节提交后要审核，播放器接口 `x/player/v2` 的 `view_points` 过审后才有值。
- 发动态：富文本框要逐行 `insertText` + `insertLineBreak`，整段插入会把第一行挪到最后；点「发布」后还有一次「确认并发送」。
