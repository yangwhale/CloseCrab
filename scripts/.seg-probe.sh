#!/bin/bash
# 语料账单探针 —— **脚本自己输出契约词**，模型只负责原样转发。
#
# 正则必须锚在**行首 + logger 名**上。理由：我自己的 Bash 工具调用也会被
# 写进同一个 nohup.out，命令行文本里带着 [语料]/ssrc= 这些串 —— 松一点的
# grep 会把探针自己的命令匹配出来当成结果 (今早就踩过一次)。
# 真账单来自 closecrab.discord_voice_sidecar，工具调用来自
# closecrab.workers.claude_code，锚住模块名两者就分开了。
#
# 2026-09-12 改成自带契约词。原因：上一版只在「有新行」时输出、没新行时
# 一个字都不打，把「输出 SKIP」这件事全交给模型自觉。Chris 那天没进语音
# 频道，探针连续 35 轮拿到空输出，模型就从执行退化成了聊天 —— 最后三轮
# 直接把 `bash .seg-probe.sh` 这条命令当文本打出来，watch-task 判定违约
# 自杀。见 memory feedback_watch-probe-degrades-to-chat。
#
# 修法不是把 prompt 写得更严，是**把判断从模型手里拿走**：长度门槛、
# 递增判定、SKIP/DONE 全在 bash 里算完，模型只剩「原样贴」一个动作。
# 连续空轮再多也不会漂，因为每轮都有确定的字面输出要转发。
#
# 两个路径都可以用环境变量覆盖 —— 不是为了灵活，是为了**能测**：
# 不覆盖就只能对着生产日志试，而生产日志里有没有长句不归我管。
L=${SEG_PROBE_LOG:-~/.claude/closecrab/bunny/nohup.out}
S=${SEG_PROBE_SEEN:-/tmp/.seg-probe-seen}
RE='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:,]+ \[bunny\] \[INFO\] closecrab\.discord_voice_sidecar: \[语料\] 第 '
touch "$S"
grep -aE "$RE" "$L" 2>/dev/null | tail -40 > /tmp/.seg-now
NEW=$(comm -13 <(sort -u "$S") <(sort -u /tmp/.seg-now))
cat /tmp/.seg-now >> "$S"

[ -z "$NEW" ] && { echo SKIP; exit 0; }

# 只要长句：秒数 ≥20 或帧数 ≥1000。短句四段各才百来帧，一个缺口就能让
# 某段看起来很吓人，纯噪音 —— 这个门槛必须在这儿卡死，不能指望模型忍住。
# 显式 gawk：三参数 match() 是 gawk 扩展，mawk 不支持。本机 /usr/bin/awk
# 现在指向 gawk，但那是 alternatives 的指向，不该拿它当契约。
echo "$NEW" | gawk '
  {
    secs = 0; frames = 0
    if (match($0, /\(([0-9.]+)s\)/, m))  secs   = m[1] + 0
    if (match($0, /([0-9]+)帧/, f))      frames = f[1] + 0
    if (secs < 20 && frames < 1000) next
    long_lines[++n] = $0
  }
  END {
    if (n == 0) { print "SKIP"; exit }
    print "DONE"
    for (i = 1; i <= n; i++) {
      print long_lines[i]
      # 唯一要判的事：分段四个百分比是不是往后递增（验 Chris 的
      # 「后半句丢得多」猜想）。判定也在这儿做完，模型不用算。
      if (match(long_lines[i], /分段([0-9]+)%\/([0-9]+)%\/([0-9]+)%\/([0-9]+)%/, p)) {
        up = (p[1] <= p[2] && p[2] <= p[3] && p[3] <= p[4])
        printf("→ 分段 %d/%d/%d/%d —— %s\n", p[1], p[2], p[3], p[4],
               up ? "确实往后递增，支持「后半句丢得多」" \
                  : "**没有**递增，不支持「后半句丢得多」")
      } else {
        print "→ 这行没有分段字段，判不了递增"
      }
    }
  }
'
