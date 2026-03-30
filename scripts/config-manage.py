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

DEFAULT_CONFIG = {
    "model": "claude-opus-4-6@default",
    "claude_bin": "~/.local/bin/claude",
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

    # Try to parse as JSON for complex values, otherwise use as string
    try:
        value = json.loads(args.value)
    except (json.JSONDecodeError, TypeError):
        value = args.value

    doc_ref.update({args.field: value})
    print(f"Set {args.field}={value} for '{args.bot_name}'")


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
