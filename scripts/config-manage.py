#!/usr/bin/env python3
"""Manage bot configurations in Firestore.

Usage:
    config-manage.py list
    config-manage.py show <bot_name>
    config-manage.py create <bot_name> --channel <type> [channel options]
    config-manage.py add-channel <bot_name> <channel_type> [channel options]
    config-manage.py set-channel <bot_name> <channel_type>
    config-manage.py set <bot_name> <field> <value>
    config-manage.py delete <bot_name>

Channel options:
    Discord:   --token TOKEN [--log-channel-id ID] [--auto-respond-channels "ID1,ID2"]
    Feishu:    --app-id ID --app-secret SECRET [--log-chat-id ID]
    Lark:      --app-id ID --app-secret SECRET
    DingTalk:  --client-id ID --client-secret SECRET

Examples:
    config-manage.py create newbot --channel discord --token "MTxx..."
    config-manage.py add-channel jarvis feishu --app-id "cli_xxx" --app-secret "xxx"
    config-manage.py set-channel jarvis discord
    config-manage.py set jarvis model claude-sonnet-4-6@default
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore

def _detect_claude_bin() -> str:
    """Auto-detect claude binary path."""
    import shutil
    return shutil.which("claude") or "~/.local/bin/claude"

DEFAULT_CONFIG = {
    "model": "claude-opus-4-7@default",
    "claude_bin": _detect_claude_bin(),
    "work_dir": "~/",
    "timeout": 600,
    "stt_engine": "gemini",
    "allowed_user_ids": [],
    "inbox": {
        "backend": "firestore",
        "project": FIRESTORE_PROJECT,
        "database": FIRESTORE_DATABASE,
    },
}


def get_db():
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def cmd_list(args):
    db = get_db()
    docs = list(db.collection("bots").stream())
    print(f"Available bots ({len(docs)}):")
    for doc in sorted(docs, key=lambda d: d.id):
        data = doc.to_dict()
        active = data.get("active_channel", "?")
        model = data.get("model", "?")
        channels = list(data.get("channels", {}).keys())
        desc = data.get("description", "")
        print(f"  {doc.id:20s}  active={active:8s}  channels={channels}  model={model[:30]}  {desc}")


def cmd_show(args):
    db = get_db()
    doc = db.collection("bots").document(args.bot_name).get()
    if not doc.exists:
        print(f"Bot '{args.bot_name}' not found")
        sys.exit(1)
    data = doc.to_dict()
    # Mask secrets for display
    masked = _mask_secrets(data)
    print(json.dumps(masked, indent=2, ensure_ascii=False, default=str))


def _mask_secrets(data: dict) -> dict:
    """Mask sensitive fields for display."""
    import copy
    d = copy.deepcopy(data)
    for ch_name, ch_cfg in d.get("channels", {}).items():
        for key in ("token", "app_secret", "client_secret"):
            if ch_cfg.get(key):
                val = ch_cfg[key]
                ch_cfg[key] = val[:8] + "..." + val[-4:] if len(val) > 16 else "***"
    email = d.get("email", {})
    if email.get("pass"):
        email["pass"] = "***"
    return d


def _parse_channel_args(channel_type: str, args) -> dict:
    """Build channel config dict from CLI args."""
    if channel_type == "discord":
        if not args.token:
            print("Error: --token is required for Discord channel")
            sys.exit(1)
        cfg = {"token": args.token}
        if args.log_channel_id:
            cfg["log_channel_id"] = args.log_channel_id
        if args.auto_respond_channels:
            cfg["auto_respond_channels"] = [s.strip() for s in args.auto_respond_channels.split(",")]
        return cfg
    elif channel_type in ("feishu", "lark"):
        if not args.app_id or not args.app_secret:
            print(f"Error: --app-id and --app-secret are required for {channel_type} channel")
            sys.exit(1)
        cfg = {"app_id": args.app_id, "app_secret": args.app_secret}
        if args.log_chat_id:
            cfg["log_chat_id"] = args.log_chat_id
        if args.allowed_open_ids:
            cfg["allowed_open_ids"] = [s.strip() for s in args.allowed_open_ids.split(",")]
        if args.auto_respond_chats:
            cfg["auto_respond_chats"] = [s.strip() for s in args.auto_respond_chats.split(",")]
        return cfg
    elif channel_type == "dingtalk":
        if not args.client_id or not args.client_secret:
            print("Error: --client-id and --client-secret are required for DingTalk channel")
            sys.exit(1)
        return {"client_id": args.client_id, "client_secret": args.client_secret}
    else:
        print(f"Error: unknown channel type '{channel_type}'")
        sys.exit(1)


def cmd_create(args):
    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    if doc_ref.get().exists:
        print(f"Bot '{args.bot_name}' already exists. Use 'add-channel' or 'set' to modify.")
        sys.exit(1)

    channel_cfg = _parse_channel_args(args.channel, args)

    doc = {
        **DEFAULT_CONFIG,
        "active_channel": args.channel,
        "description": args.description or "",
        "guild_id": args.guild_id or "",
        "channels": {args.channel: channel_cfg},
    }

    doc_ref.set(doc)
    print(f"Created bot '{args.bot_name}' with {args.channel} channel")
    print(json.dumps(_mask_secrets(doc), indent=2, ensure_ascii=False, default=str))


def cmd_add_channel(args):
    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    doc = doc_ref.get()
    if not doc.exists:
        print(f"Bot '{args.bot_name}' not found. Use 'create' first.")
        sys.exit(1)

    channel_cfg = _parse_channel_args(args.channel_type, args)

    doc_ref.update({f"channels.{args.channel_type}": channel_cfg})
    print(f"Added {args.channel_type} channel to '{args.bot_name}'")


def cmd_set_channel(args):
    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    doc = doc_ref.get()
    if not doc.exists:
        print(f"Bot '{args.bot_name}' not found")
        sys.exit(1)

    data = doc.to_dict()
    if args.channel_type not in data.get("channels", {}):
        available = list(data.get("channels", {}).keys())
        print(f"Error: channel '{args.channel_type}' not configured. Available: {available}")
        sys.exit(1)

    doc_ref.update({"active_channel": args.channel_type})
    print(f"Switched '{args.bot_name}' to {args.channel_type}")


def cmd_set(args):
    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    if not doc_ref.get().exists:
        print(f"Bot '{args.bot_name}' not found")
        sys.exit(1)

    # 防呆: 直接 set 整个 livekit 字段会抹掉 bot 启动时回写的 hmac_secret,
    # 导致 frontend 验签失败. 强制走 set-livekit 子命令.
    if args.field == "livekit":
        print("ERROR: 不要用 'set livekit' 整体覆盖 livekit 字段 (会抹掉 bot 回写的 hmac_secret).")
        print("       改用 'set-livekit' 子命令, 它会保留 hmac_secret. 例:")
        print("       python3 scripts/config-manage.py set-livekit", args.bot_name,
              "--auto-detect --frontend-url https://... --vertex-project ... --enable")
        sys.exit(1)

    # Try to parse as JSON for complex values, otherwise use as string
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, TypeError):
        value = args.value

    doc_ref.update({args.field: value})
    print(f"Set {args.field}={value} for '{args.bot_name}'")


def cmd_set_livekit(args):
    """Write bots/{bot_name}.livekit field for voice IO.

    Two ways to provide credentials:
      1. --auto-detect: read from ~/livekit-server/.api_key / .api_secret (set by install-livekit.sh)
      2. --api-key / --api-secret: explicit values

    HMAC secret is NOT set here — bot generates it on first start and writes back to Firestore.
    """
    from pathlib import Path

    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    if not doc_ref.get().exists:
        print(f"Bot '{args.bot_name}' not found")
        sys.exit(1)

    # 拿 API key/secret
    if args.auto_detect:
        key_file = Path.home() / "livekit-server" / ".api_key"
        secret_file = Path.home() / "livekit-server" / ".api_secret"
        if not key_file.exists() or not secret_file.exists():
            print(f"--auto-detect 失败: {key_file} 或 {secret_file} 不存在")
            print("先在本机跑 ./scripts/install-livekit.sh 装 LiveKit infra")
            sys.exit(1)
        api_key = key_file.read_text().strip()
        api_secret = secret_file.read_text().strip()
        # 不打印 api_key 全文 (会进 shell history / SSH 日志). 显示前 8 字符即可.
        print(f"自动检测到 API key: {api_key[:8]}... (已读自 {key_file})")
    else:
        if not args.api_key or not args.api_secret:
            print("必须指定 --auto-detect 或 (--api-key + --api-secret)")
            sys.exit(1)
        api_key = args.api_key
        api_secret = args.api_secret

    # url (signaling) 默认从 frontend domain 推 (live.x.com → wss://livekit.x.com 或 ws://127.0.0.1:7880)
    url = args.url or "ws://127.0.0.1:7880"

    livekit_cfg = {
        "url": url,
        "api_key": api_key,
        "api_secret": api_secret,
        "frontend_url": args.frontend_url,
        "enabled": args.enable,
    }
    if args.vertex_project:
        livekit_cfg["vertex_project"] = args.vertex_project
    if args.vertex_location:
        livekit_cfg["vertex_location"] = args.vertex_location

    # merge — 不覆盖已存在的 hmac_secret (bot 启动时生成的, 不要踩)
    existing = doc_ref.get().to_dict().get("livekit", {}) or {}
    if "hmac_secret" in existing:
        livekit_cfg["hmac_secret"] = existing["hmac_secret"]
        print(f"保留已有 hmac_secret (前 8 字符: {existing['hmac_secret'][:8]}...)")

    doc_ref.update({"livekit": livekit_cfg})

    print(f"\n已写入 bots/{args.bot_name}.livekit:")
    # 显式 mask 敏感字段 (不能用 'in k' 判断: api_key 不含 "secret" 但同样敏感)
    SENSITIVE_FIELDS = {"api_key", "api_secret", "hmac_secret"}
    masked = {k: ("***" if k in SENSITIVE_FIELDS else v) for k, v in livekit_cfg.items()}
    print(json.dumps(masked, indent=2, ensure_ascii=False))
    if args.enable:
        print(f"\nVoice 已启用. 重启 bot 后, 在飞书私聊发 /voice 验证.")
        if "hmac_secret" not in livekit_cfg:
            print(f"(首次启动时 bot 会自动生成 hmac_secret 并回写 Firestore)")


def cmd_delete(args):
    db = get_db()
    doc_ref = db.collection("bots").document(args.bot_name)
    if not doc_ref.get().exists:
        print(f"Bot '{args.bot_name}' not found")
        sys.exit(1)

    if not args.yes:
        confirm = input(f"Delete bot '{args.bot_name}'? (yes/no): ")
        if confirm.lower() != "yes":
            print("Cancelled")
            return

    doc_ref.delete()
    print(f"Deleted bot '{args.bot_name}'")


def main():
    parser = argparse.ArgumentParser(description="Manage bot configs in Firestore")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list", help="List all bots")

    # show
    p_show = subparsers.add_parser("show", help="Show bot config")
    p_show.add_argument("bot_name")

    # create
    p_create = subparsers.add_parser("create", help="Create a new bot")
    p_create.add_argument("bot_name")
    p_create.add_argument("--channel", required=True, choices=["discord", "feishu", "lark", "dingtalk"])
    p_create.add_argument("--description", default="")
    p_create.add_argument("--guild-id", default="")
    # Channel-specific args (shared across create/add-channel)
    for p in [p_create]:
        _add_channel_args(p)

    # add-channel
    p_add = subparsers.add_parser("add-channel", help="Add a channel to existing bot")
    p_add.add_argument("bot_name")
    p_add.add_argument("channel_type", choices=["discord", "feishu", "lark", "dingtalk"])
    _add_channel_args(p_add)

    # set-channel
    p_switch = subparsers.add_parser("set-channel", help="Switch active channel")
    p_switch.add_argument("bot_name")
    p_switch.add_argument("channel_type")

    # set
    p_set = subparsers.add_parser("set", help="Set a config field")
    p_set.add_argument("bot_name")
    p_set.add_argument("field")
    p_set.add_argument("value")

    # set-livekit (voice IO 配置)
    p_lk = subparsers.add_parser(
        "set-livekit",
        help="Configure voice IO (LiveKit) for a bot — writes bots/{name}.livekit"
    )
    p_lk.add_argument("bot_name")
    p_lk.add_argument("--frontend-url", required=True,
                      help="Frontend URL, e.g. https://live.example.com (用户飞书 /voice 命令拿到的链接 host)")
    p_lk.add_argument("--auto-detect", action="store_true",
                      help="从本机 ~/livekit-server/.api_key / .api_secret 自动读取凭据 "
                           "(推荐, 避免人工拷贝长字符串)")
    p_lk.add_argument("--api-key", help="LiveKit API key (--auto-detect 时不用)")
    p_lk.add_argument("--api-secret", help="LiveKit API secret (--auto-detect 时不用)")
    p_lk.add_argument("--url", default="",
                      help="Server signaling URL, 默认 ws://127.0.0.1:7880 "
                           "(bot 和 livekit-server 同机时用 localhost 即可)")
    p_lk.add_argument("--vertex-project", default="",
                      help="GCP Vertex AI project (Gemini STT/TTS 用), 默认从 GOOGLE_CLOUD_PROJECT 推")
    p_lk.add_argument("--vertex-location", default="",
                      help="Vertex region, 默认 global")
    p_lk.add_argument("--enable", action="store_true",
                      help="启用 voice IO (bot 启动时会拉起 LiveKit worker)")

    # delete
    p_del = subparsers.add_parser("delete", help="Delete a bot")
    p_del.add_argument("bot_name")
    p_del.add_argument("--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    commands = {
        "list": cmd_list,
        "show": cmd_show,
        "create": cmd_create,
        "add-channel": cmd_add_channel,
        "set-channel": cmd_set_channel,
        "set": cmd_set,
        "set-livekit": cmd_set_livekit,
        "delete": cmd_delete,
    }
    commands[args.command](args)


def _add_channel_args(parser):
    """Add channel-specific arguments to a parser."""
    parser.add_argument("--token", help="Discord bot token")
    parser.add_argument("--app-id", help="Feishu/Lark App ID")
    parser.add_argument("--app-secret", help="Feishu/Lark App Secret")
    parser.add_argument("--client-id", help="DingTalk Client ID")
    parser.add_argument("--client-secret", help="DingTalk Client Secret")
    parser.add_argument("--log-channel-id", help="Discord log channel ID")
    parser.add_argument("--log-chat-id", help="Feishu/Lark log chat ID")
    parser.add_argument("--auto-respond-channels", help="Discord auto-respond channel IDs (comma-separated)")
    parser.add_argument("--auto-respond-chats", help="Feishu auto-respond chat IDs (comma-separated)")
    parser.add_argument("--allowed-open-ids", help="Feishu/Lark allowed open IDs (comma-separated)")


if __name__ == "__main__":
    main()
