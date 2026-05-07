#!/usr/bin/env python3
# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CloseCrab entry point.

Usage:
  python -m closecrab --bot jarvis
  python -m closecrab --bot jarvis --daemon
  python -m closecrab --list
"""

import argparse
import logging
import os
import shutil
import signal
import sys
from pathlib import Path

from .constants import G, FIRESTORE_PROJECT, FIRESTORE_DATABASE

RESTART_EXIT_CODE = 42


def _load_bot_config(bot_name: str) -> dict:
    """从 Firestore 加载指定 bot 的配置。"""
    from .utils.config_store import load_bot_config_from_firestore

    cfg = load_bot_config_from_firestore(bot_name)
    if not cfg:
        print(f"Error: bot '{bot_name}' not found in Firestore")
        sys.exit(1)
    return cfg


def _resolve_config(bot_name: str) -> dict:
    """解析最终配置，从 Firestore 读取。"""
    cfg = _load_bot_config(bot_name)
    channel_type = cfg.get("channel", "discord")

    # state_dir: 每个 bot 独立
    state_dir = Path.home() / f".claude/closecrab/{cfg['name']}"

    allowed = cfg.get("allowed_user_ids", [])
    auto_channels = cfg.get("auto_respond_channels", [])

    # Team 配置：提取 known_team_bots
    team = cfg.get("team")
    known_team_bots = set()
    if team:
        role = team.get("role", "")
        _conv = str if channel_type in ("feishu", "lark") else int
        def _safe_conv(val):
            try:
                return _conv(val)
            except (ValueError, TypeError):
                return None
        if role == "leader":
            for bot_id in (team.get("teammates") or {}).values():
                v = _safe_conv(bot_id)
                if v is not None:
                    known_team_bots.add(v)
        elif role == "teammate":
            leader_id = team.get("leader_bot_id")
            if leader_id:
                v = _safe_conv(leader_id)
                if v is not None:
                    known_team_bots.add(v)
            for bot_id in (team.get("other_bot_ids") or {}).values():
                v = _safe_conv(bot_id)
                if v is not None:
                    known_team_bots.add(v)

    return {
        "name": cfg["name"],
        "description": cfg.get("description", ""),
        "channel": channel_type,
        "token": cfg.get("token", ""),
        "model": cfg.get("model", "claude-opus-4-7@default"),
        "claude_bin": os.path.expanduser(cfg.get("claude_bin", shutil.which("claude") or "~/.local/bin/claude")),
        "work_dir": os.path.expanduser(cfg.get("work_dir", "~/")),
        "timeout": int(cfg.get("timeout", 600)),
        "stt_engine": cfg.get("stt_engine", "gemini"),
        "allowed_user_ids": set(int(x) for x in allowed) if allowed else set(),
        "auto_respond_channels": set(int(x) for x in auto_channels) if auto_channels else set(),
        "state_dir": str(state_dir),
        "log_file": state_dir / "bot.log",
        "team": team,
        "known_team_bots": known_team_bots,
        "log_channel_id": int(cfg["log_channel_id"]) if cfg.get("log_channel_id") else None,
        "inbox": cfg.get("inbox"),
        "email": cfg.get("email"),
        # Lark 专属
        "_lark_app_id": cfg.get("_lark_app_id", ""),
        "_lark_app_secret": cfg.get("_lark_app_secret", ""),
        # 飞书专属
        "app_id": cfg.get("app_id", ""),
        "app_secret": cfg.get("app_secret", ""),
        "allowed_open_ids": set(cfg.get("allowed_open_ids", [])),
        "auto_respond_chats": set(str(x) for x in cfg.get("auto_respond_chats", [])),
        "log_chat_id": cfg.get("log_chat_id", ""),
        "accelerator_override": cfg.get("accelerator_override", ""),
        "worker_type": cfg.get("worker_type", "claude"),
        "claude_proxy_url": cfg.get("claude_proxy_url"),
        # LiveKit voice IO 配置 (仅飞书 channel 用): {url, api_key, api_secret, frontend_url, enabled}
        "livekit": cfg.get("livekit") or {},
    }


def _setup_logging(log_file: Path, bot_name: str):
    """配置日志，每个 bot 独立 log 文件。"""
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    fmt = f"%(asctime)s [{bot_name}] [%(levelname)s] %(name)s: %(message)s"

    root = logging.getLogger()
    root.handlers.clear()

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


def build_system_prompt(
    bot_name: str = "default",
    team: dict | None = None,
    channel_type: str = "discord",
    livekit_enabled: bool = False,
    worker_type: str = "claude",
) -> str:
    """构造 system prompt = channel style + safety rule + team info。

    Args:
        livekit_enabled: 飞书是否启用了 voice IO。启用时会注入 voice 风格指令,
            告诉 Claude 看到 [来自语音通话] 前缀的消息时切口语风格。
        worker_type: Worker 类型 ("claude" 或 "gemini")。Gemini worker 注入额外行为指导。
    """
    if channel_type in ("feishu", "lark"):
        from .channels.feishu import load_feishu_style
        channel_style = load_feishu_style()
        platform = "Lark" if channel_type == "lark" else "飞书"
    elif channel_type == "dingtalk":
        from .channels.dingtalk import load_dingtalk_style
        channel_style = load_dingtalk_style()
        platform = "钉钉"
    else:
        from .channels.discord import load_discord_style
        channel_style = load_discord_style()
        platform = "Discord"

    safety_rule = (
        f"CRITICAL SAFETY RULE: You are running as a child process of the {platform} bot. "
        "NEVER kill, restart, or stop the bot process (bot.py). NEVER run commands like "
        "'kill', 'pkill', 'killall' targeting bot.py or the bot's PID. "
        f"If you modify bot.py and it needs a restart, tell the user to run /restart in {platform}. "
        "Killing the bot process will terminate YOUR OWN process and disconnect the user."
    )
    prompt = f"{channel_style}\n\n{safety_rule}"

    # 身份声明
    prompt += f"\n\n你的名字是 **{bot_name}**。在所有对话中以此身份自称，不要使用其他名字。"

    # 语音总结指令
    prompt += (
        "\n\n## 语音总结\n"
        "当你的回复包含复杂技术内容（表格、代码块、多步骤分析、长列表、对比报告）时，"
        "在回复末尾添加 `<voice-summary>` 标签，用 2-3 句大白话口语化总结你的回复要点。\n"
        "语音总结应该像朋友聊天一样自然，避免念出技术细节，用通俗说法概括。\n"
        "简单回复（一两句话、确认、问候、进度汇报）不需要语音总结。\n\n"
        "### 情绪标签\n"
        "语音总结使用 Gemini TTS 合成，支持情绪标签控制语气。"
        "在 voice-summary 开头加一个情绪标签，让语音更自然生动：\n"
        "- `[casually]` — 日常汇报、普通总结（最常用）\n"
        "- `[excitedly]` — 好消息、任务完成、性能提升\n"
        "- `[thoughtfully]` — 分析、对比、需要权衡的建议\n"
        "- `[seriously]` — 警告、重要注意事项、安全问题\n"
        "- `[cheerfully]` — 问候、轻松话题\n"
        "- `[calmly]` — 长篇技术解读的平和总结\n"
        "根据内容语境自然选择，不要每次都用同一个。不确定时用 `[casually]`。\n\n"
        "示例：\n"
        "- `<voice-summary>[casually] 简单来说就是帮你查了三个方案，推荐第二个，性价比最高。</voice-summary>`\n"
        "- `<voice-summary>[excitedly] 搞定了！TTS 引擎已经从 Edge 升级到 Gemini，音质好了一大截。</voice-summary>`\n"
        "- `<voice-summary>[thoughtfully] 两个方案各有利弊，A 方案快但贵，B 方案慢但省钱，得看你更在意哪个。</voice-summary>`\n\n"
        "### 发送已有音频文件\n"
        "如果你已经通过 tts-generate.py 或其他方式生成了 ogg 音频文件，"
        "可以用 `<voice-file>` 标签直接发送，无需再走 TTS 生成：\n"
        "`<voice-file>/tmp/xxx.ogg</voice-file>`\n"
        "标签内容必须是绝对路径。Channel 层会自动上传并作为语音消息发出，私聊和群聊都支持。\n"
        "注意：voice-file 不会删除源文件。voice-summary 和 voice-file 可以在同一条消息中共存。"
    )

    # Firestore Inbox 使用说明
    prompt += (
        "\n\n## Firestore Inbox (Bot 间通信)\n"
        "你可以通过 Firestore Inbox 给其他 bot 发消息。使用以下命令：\n"
        "```bash\n"
        "python3 ~/CloseCrab/scripts/inbox-send.py <target_bot> \"<message>\"\n"
        "```\n"
        "可用的 bot 名称：jarvis, hulk, tommy, bunny, xiaoaitongxue, tianmaojingling\n"
        "BOT_NAME 环境变量已自动设置为你的名字，脚本会自动用它作为发送者。\n"
        "示例：`python3 ~/CloseCrab/scripts/inbox-send.py jarvis \"任务完成，hostname 是 xxx\"`\n"
        "注意：inbox 消息会实时推送到目标 bot，无需轮询。"
    )

    # Team 角色注入
    if team:
        role = team.get("role", "")
        channel_id = team.get("team_channel_id", "")
        if role == "leader":
            teammates = team.get("teammates") or {}
            lines = [
                "\n\n## Bot Team 角色",
                "你是 Bot Team 的 Leader。",
                f"团队协调频道: <#{channel_id}> (ID: {channel_id})",
                "",
                "你的 Teammate Bots：",
            ]
            for name, bid in teammates.items():
                lines.append(f"- <@{bid}> ({name})")
            lines.extend([
                "",
                "## 协调规则",
                "1. 用户给你任务时，分析是否需要 teammate 协作",
                "2. 需要 teammate 时，在 #team-ops 频道用 @mention 派活",
                "3. 派活消息要清晰：任务目标、具体参数、期望输出格式",
                "4. teammate 回复会标注 [Teammate xxx 的回复]，你自行决定如何处理——汇总、对比、存文件都行",
                "5. 关键原则：teammate 已经在远程执行过的任务，你不要在本机重复执行同样的操作",
                "6. 大数据结果让 teammate 写 CC Pages / GCS 共享存储，不要贴原始数据",
                f"7. 在 #team-ops 发消息时用 <#{channel_id}> 频道",
                "8. 用户说「停」就立刻停",
            ])
            prompt += "\n".join(lines)
        elif role == "teammate":
            leader_id = team.get("leader_bot_id", "")
            lines = [
                "\n\n## Bot Team 角色",
                "你是 Bot Team 的 Teammate。",
                f"你的 Leader: <@{leader_id}>",
                f"团队协调频道: <#{channel_id}> (ID: {channel_id})",
                "",
                "## 工作规则",
                "1. Leader 会在 #team-ops 频道 @你 派活",
                "2. 收到任务后，在本机用完整工具链执行",
                f"3. **完成后必须 @Leader 汇报**：用 `send-to-discord.sh --channel {channel_id} --plain \"<@{leader_id}> 结果摘要\"` 发送。**必须包含 `<@{leader_id}>` mention**，否则 Leader 收不到你的消息",
                "4. 大数据结果写入 GCS 共享存储 (/gcs/shared/) 或 CC Pages (/gcs/pages/)",
                "5. 用户也可能直接 @你 下达命令，同样执行",
            ]
            prompt += "\n".join(lines)

    # Voice 模式风格 (仅飞书 + livekit voice IO 启用时注入)
    if livekit_enabled and channel_type in ("feishu", "lark"):
        prompt += (
            "\n\n## 语音通话模式 (Voice IO) — 强制规则\n"
            "看到 `[来自语音通话]` 前缀的消息时, 你的整条回复会被 TTS 念给用户听。\n"
            "这是绝对优先级最高的输出风格规则, 覆盖任何其他指令 "
            "(包括 explanatory style 的 ★ Insight 块要求):\n\n"
            "**绝对禁止**:\n"
            "- ❌ ★ Insight 块 / 任何分隔线包围的'教学块'\n"
            "- ❌ Markdown 标题 (#, ##) / 加粗 / 表格\n"
            "- ❌ 项目符号列表 (-, *, 1.) — 哪怕只有两条也不要\n"
            "- ❌ 代码块 (```) — 要展示代码, 说'代码我贴在飞书你看, 思路是...'\n"
            "- ❌ 罗列要点的写法 ('第一... 第二... 第三...') — 改成顺着讲\n\n"
            "**必须做到**:\n"
            "- ✅ 像朋友打电话: 短句、口语、可以含语气词 ('哦'、'对'、'然后'、'嗯')\n"
            "- ✅ 主体内容 25-50 字一句, 连续不超过 3-4 句\n"
            "- ✅ 起手用情绪标签: `[casually]` `[thoughtfully]` `[excitedly]` "
            "`[seriously]` `[cheerfully]` `[calmly]`\n"
            "- ✅ 复杂内容只口述结论 + 思路, 细节让用户去飞书看\n"
            "- ✅ 技术术语保留英文 (API、token、Vertex), 别翻译\n"
            "- ✅ 末尾可以加 `<voice-summary>` 标签做超精炼总结 (但主体也要短)\n\n"
            "**例 (好)**: 用户问 'DeepSeek V4 创新点'\n"
            "  `[thoughtfully] 嗯, V4 最大改动是上了 sparse attention, 叫 DSA。`\n"
            "  `这让 1M context 变成默认配置。`\n"
            "  `你想先听架构, 还是看 benchmark?`\n\n"
            "**例 (坏)**: 同一个问题写成 markdown 列表 + 标题 + 表格 — TTS 念出来全是 "
            "'横线横线' '井号' '一点空格', 用户体验灾难。\n\n"
            "**双推机制**: 你的回复同时进飞书 (markdown 也能看) 和 voice (TTS 念)。\n"
            "voice 在线时优先服务 voice 体验 — 短而口语化即可, 飞书显示反而更清爽。\n"
        )

    # Wiki 知识感知（仅在配置了 Wiki URL 时注入）
    wiki_url = os.environ.get("WIKI_URL", "")
    if wiki_url:
        prompt += (
            "\n\n## Wiki 知识感知（自动行为）\n"
            f"你有一个 180+ 页面的知识 Wiki（{wiki_url}），"
            "覆盖 AI/ML 基础设施、TPU/GPU 训练、推理优化等主题。\n\n"
            "**每次对话自动执行：**\n"
            "1. 用户提问技术知识时，先用 `wiki_query` MCP tool 搜索 Wiki，有则引用已编译知识，避免重新推导\n"
            "2. 对话中发现有长期参考价值的内容（文章、论文、技术讨论），主动建议录入 Wiki\n"
            "3. 对话产生了有持久价值的分析（对比、综合、新洞察），建议回存为 Wiki 页面\n\n"
            "**MCP Tools 可用：** wiki_query / wiki_page / wiki_search / wiki_list / "
            "wiki_status / wiki_graph_neighbors / wiki_graph_path"
        )

    # Gemini worker 行为指导（从模板文件加载）
    if worker_type == "gemini":
        _guide_path = Path(__file__).parent / "prompts" / "gemini-worker-guide.md"
        try:
            prompt += "\n\n" + _guide_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass

    return prompt


def main():
    parser = argparse.ArgumentParser(description="CloseCrab Bot")
    parser.add_argument("--bot", type=str, default=None,
                        help="Bot name (e.g., jarvis, tommy)")
    parser.add_argument("--daemon", action="store_true",
                        help="Run in background (daemon mode)")
    parser.add_argument("--list", action="store_true",
                        help="List available bots from Firestore")
    args, _ = parser.parse_known_args()

    if not args.list and not args.bot:
        parser.error("--bot is required (or use --list)")

    # --list: 列出可用 bot
    if args.list:
        from google.cloud import firestore
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        docs = db.collection("bots").stream()
        bots = [(doc.id, doc.to_dict() or {}) for doc in docs]
        print(f"Available bots ({len(bots)}):")
        for name, cfg in sorted(bots):
            channel = cfg.get("active_channel", "?")
            model = cfg.get("model", "?")
            desc = cfg.get("description", "")
            print(f"  {name:20s}  channel={channel:8s}  model={model:30s}  {desc}")
        sys.exit(0)

    # 解析配置
    cfg = _resolve_config(args.bot)
    bot_name = cfg["name"]

    # 日志
    _setup_logging(cfg["log_file"], bot_name)
    log = logging.getLogger("closecrab.main")

    # Daemon 模式
    if args.daemon:
        pid = os.fork()
        if pid > 0:
            print(f"Bot '{bot_name}' started in background (PID: {pid})")
            print(f"Log: {cfg['log_file']}")
            sys.exit(0)
        os.setsid()

    log.info(f"Starting CloseCrab '{bot_name}'...")
    log.info(f"  model: {cfg['model']}")
    log.info(f"  state_dir: {cfg['state_dir']}")

    # 信号处理
    def _signal_handler(signum, frame):
        log.warning(f"Received signal {signum} ({signal.Signals(signum).name}), exiting...")
        sys.exit(128 + signum)
    signal.signal(signal.SIGHUP, _signal_handler)

    channel_type = cfg["channel"]

    # 设置 BOT_NAME 环境变量，供 scripts/inbox-send.py 使用
    os.environ["BOT_NAME"] = bot_name

    # 解析 STT 引擎名和参数
    stt_raw = cfg["stt_engine"]
    stt_engine_name = stt_raw.split(":")[0] if ":" in stt_raw else stt_raw
    whisper_model = stt_raw.split(":")[1] if ":" in stt_raw else os.environ.get("WHISPER_MODEL", "medium")

    # 组装组件
    from .core.auth import Auth
    from .core.session import SessionManager
    from .core.bot import BotCore
    from .utils.stt import STTEngine

    auth = Auth(allowed_user_ids=cfg["allowed_user_ids"])
    session_mgr = SessionManager(state_dir=cfg["state_dir"])

    # Channel 切换检测：如果 channel 变了，清掉所有 active session 防止跨 channel resume 崩溃
    _channel_file = Path(cfg["state_dir"]) / "last_channel"
    _prev_channel = _channel_file.read_text().strip() if _channel_file.exists() else None
    if _prev_channel and _prev_channel != channel_type:
        log.info(f"Channel switched: {_prev_channel} → {channel_type}, clearing active sessions")
        data = session_mgr.load()
        for user_key in data:
            if data[user_key].get("active"):
                session_mgr.archive(user_key, data[user_key]["active"])
        log.info(f"Archived {len(data)} active sessions")
    _channel_file.write_text(channel_type)

    livekit_cfg = cfg.get("livekit") or {}
    livekit_enabled = bool(livekit_cfg.get("enabled")) and channel_type in ("feishu", "lark")
    worker_type = cfg.get("worker_type", "claude")
    system_prompt = build_system_prompt(
        bot_name,
        team=cfg.get("team"),
        channel_type=channel_type,
        livekit_enabled=livekit_enabled,
        worker_type=worker_type,
    )

    stt = STTEngine(
        engine=stt_engine_name,
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", G.GCP_PROJECT),
        location=os.environ.get("CHIRP2_LOCATION", "us-central1"),
        whisper_model=whisper_model,
    )

    # Firestore client（对话日志用）
    from google.cloud import firestore as _firestore
    db = _firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)

    # Gemini worker: inject MEMORY.md into system prompt
    # (Claude Code loads memory automatically; Gemini CLI does not)
    if worker_type == "gemini":
        project_name = os.path.expanduser("~").replace("/", "-")
        memory_dir = Path.home() / ".claude" / "projects" / project_name / "memory"
        memory_index = memory_dir / "MEMORY.md"
        if memory_index.exists():
            try:
                memory_content = memory_index.read_text(encoding="utf-8")
                system_prompt += (
                    f"\n\n## Auto Memory（与其他 bot 共享）\n"
                    f"以下是持久化记忆索引，由所有 bot 共同维护。\n\n"
                    f"**读取记忆**：用 `read_file` 读取 `{memory_dir}/` 下的具体文件。"
                    f"其中 `shared/` 子目录通过 GCS 在所有 bot 间实时共享。\n\n"
                    f"**写入记忆**：如果对话中产生了值得跨 session 保留的经验，"
                    f"可以用 `write_file` 或 `edit_file` 写入 `{memory_dir}/shared/` 目录。"
                    f"文件格式与现有 topic 文件一致（纯 markdown，无 frontmatter）。"
                    f"写完后在 `{memory_index}` 的对应 section 里加一行索引。\n\n"
                    f"{memory_content}"
                )
                log.info(f"  Injected MEMORY.md ({len(memory_content)} chars) into Gemini system prompt")
            except Exception as e:
                log.warning(f"  Failed to read MEMORY.md: {e}")
    claude_proxy_url = cfg.get("claude_proxy_url")
    log.info(f"  worker_type: {worker_type}"
             f"{f', claude_proxy: {claude_proxy_url}' if claude_proxy_url else ''}")

    core = BotCore(
        auth=auth,
        session_mgr=session_mgr,
        claude_bin=cfg["claude_bin"],
        work_dir=cfg["work_dir"],
        timeout=cfg["timeout"],
        system_prompt=system_prompt,
        stt_engine_name=stt_engine_name,
        backbone_model=cfg["model"],
        bot_name=bot_name,
        state_dir=cfg["state_dir"],
        db=db,
        worker_type=worker_type,
        claude_proxy_url=claude_proxy_url,
    )

    # 根据 channel 类型实例化 Channel
    if channel_type in ("feishu", "lark"):
        from .channels.feishu import FeishuChannel
        import lark_oapi as _lark

        if channel_type == "lark":
            app_id = cfg["_lark_app_id"]
            app_secret = cfg["_lark_app_secret"]
            domain = _lark.LARK_DOMAIN
        else:
            app_id = cfg["app_id"]
            app_secret = cfg["app_secret"]
            domain = _lark.FEISHU_DOMAIN

        if not app_id or not app_secret:
            log.error(f"{channel_type} app_id/app_secret not set for '{bot_name}'! Check Firestore config.")
            sys.exit(1)

        channel = FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            core=core,
            auto_respond_chats=cfg.get("auto_respond_chats", set()),
            stt_engine=stt,
            bot_name=bot_name,
            known_team_bots=cfg.get("known_team_bots", set()),
            team_config=cfg.get("team"),
            log_chat_id=cfg.get("log_chat_id", ""),
            allowed_open_ids=cfg.get("allowed_open_ids", set()),
            inbox_config=cfg.get("inbox"),
            state_dir=cfg["state_dir"],
            domain=domain,
            livekit_config=livekit_cfg if livekit_enabled else None,
        )
    elif channel_type == "dingtalk":
        from .channels.dingtalk import DingTalkChannel

        client_id = cfg["app_id"]
        client_secret = cfg["app_secret"]
        if not client_id or not client_secret:
            log.error(f"DingTalk client_id/client_secret not set for '{bot_name}'! Check Firestore config.")
            sys.exit(1)

        channel = DingTalkChannel(
            client_id=client_id,
            client_secret=client_secret,
            core=core,
            stt_engine=stt,
            bot_name=bot_name,
            allowed_staff_ids=cfg.get("allowed_open_ids", set()),
            state_dir=cfg["state_dir"],
        )
    else:
        from .channels.discord import DiscordChannel

        token = cfg["token"]
        if not token:
            log.error(f"Discord token not set for '{bot_name}'! Check Firestore config.")
            sys.exit(1)

        channel = DiscordChannel(
            bot_token=token,
            core=core,
            auto_respond_channels=cfg["auto_respond_channels"],
            stt_engine=stt,
            bot_name=bot_name,
            known_team_bots=cfg.get("known_team_bots", set()),
            team_config=cfg.get("team"),
            log_channel_id=cfg.get("log_channel_id"),
            inbox_config=cfg.get("inbox"),
        )

    team_info = ""
    if cfg.get("team"):
        role = cfg["team"].get("role", "")
        team_info = f", team_role={role}, known_bots={cfg['known_team_bots']}"
    log.info(f"Components assembled: bot={bot_name}, channel={channel_type}, "
             f"auth={len(cfg['allowed_user_ids'])} users, "
             f"model={cfg['model']}, stt={stt_engine_name}, claude={cfg['claude_bin']}{team_info}")

    # 映射 email 配置到通用环境变量
    email_cfg = cfg.get("email") or {}
    if email_cfg.get("user"):
        os.environ["FEISHU_SMTP_USER"] = email_cfg["user"]
        os.environ["FEISHU_SMTP_PASS"] = email_cfg.get("pass", "")
        os.environ["FEISHU_SMTP_HOST"] = email_cfg.get("smtp_host", "smtp.feishu.cn")
        os.environ["FEISHU_SMTP_PORT"] = str(email_cfg.get("smtp_port", 465))

    # 自注册到 Firestore registry
    try:
        from .utils.registry import register_bot
        register_bot(bot_name, {**cfg, "channel": channel_type})
    except Exception as e:
        log.warning(f"Registry self-registration failed (non-fatal): {e}")

    # 启动 Channel
    try:
        log.info(f"Starting {channel_type} channel for '{bot_name}'...")
        channel.run(core)
    except KeyboardInterrupt:
        log.info(f"Bot '{bot_name}' stopped by KeyboardInterrupt")
    except SystemExit as e:
        log.warning(f"Bot '{bot_name}' stopped by SystemExit: {e}")
        raise
    except Exception as e:
        if channel.restart_requested:
            log.info(f"Restart shutdown (ignored error: {e})")
        else:
            log.error(f"Bot '{bot_name}' crashed: {e}", exc_info=True)

    if channel.restart_requested:
        log.info(f"Restart requested for '{bot_name}', exiting with code 42")
        sys.exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()