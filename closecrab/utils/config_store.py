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

"""Load bot configuration from Firestore.

Firestore database: configured via FIRESTORE_PROJECT / FIRESTORE_DATABASE
Collection: bots/{bot_name}

Document structure:
    active_channel: str         # "discord" | "feishu" | "lark" | "dingtalk"
    model: str
    claude_bin: str
    work_dir: str
    timeout: int
    stt_engine: str
    guild_id: str
    allowed_user_ids: list[int]
    channels:
        discord:
            token: str
            auto_respond_channels: list[str]
            log_channel_id: str
        feishu:
            app_id: str
            app_secret: str
        lark:
            app_id: str
            app_secret: str
        dingtalk:
            client_id: str
            client_secret: str
    email:
        smtp_host, smtp_port, imap_host, imap_port, user, pass
    team:
        role, team_channel_id, teammates / leader_bot_id / other_bot_ids
    inbox:
        project, database
"""

import logging

from google.cloud import firestore

from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE

log = logging.getLogger(__name__)


def load_bot_config_from_firestore(bot_name: str) -> dict | None:
    """Load bot config from Firestore, return None if not found.

    Returns a dict compatible with the format expected by _resolve_config():
    - active_channel is mapped to "channel"
    - channel-specific fields are flattened to top level
    - token/secret values are actual values (not env var names)
    """
    try:
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("bots").document(bot_name).get()
    except Exception as e:
        log.warning(f"Firestore config read failed for '{bot_name}': {e}")
        return None

    if not doc.exists:
        log.info(f"Bot '{bot_name}' not found in Firestore")
        return None

    data = doc.to_dict() or {}
    active_channel = data.get("active_channel", "discord")
    channels = data.get("channels", {})
    channel_cfg = channels.get(active_channel, {})

    # Build config dict compatible with existing _resolve_config
    cfg = {
        "name": bot_name,
        "description": data.get("description", ""),
        "channel": active_channel,
        "model": data.get("model", "claude-opus-4-6@default"),
        "claude_bin": data.get("claude_bin", "~/.local/bin/claude"),
        "work_dir": data.get("work_dir", "~/"),
        "timeout": data.get("timeout", 600),
        "stt_engine": data.get("stt_engine", "gemini"),
        "guild_id": data.get("guild_id", ""),
        "allowed_user_ids": data.get("allowed_user_ids", []),
        "team": data.get("team"),
        "inbox": data.get("inbox"),
        "email": data.get("email"),
    }

    # Flatten channel-specific config to top level
    if active_channel == "discord":
        cfg["token"] = channel_cfg.get("token", "")
        cfg["auto_respond_channels"] = channel_cfg.get("auto_respond_channels", [])
        cfg["log_channel_id"] = channel_cfg.get("log_channel_id", "")
    elif active_channel == "feishu":
        cfg["app_id"] = channel_cfg.get("app_id", "")
        cfg["app_secret"] = channel_cfg.get("app_secret", "")
        cfg["allowed_open_ids"] = channel_cfg.get("allowed_open_ids", [])
        cfg["auto_respond_chats"] = channel_cfg.get("auto_respond_chats", [])
        cfg["log_chat_id"] = channel_cfg.get("log_chat_id", "")
    elif active_channel == "lark":
        cfg["_lark_app_id"] = channel_cfg.get("app_id", "")
        cfg["_lark_app_secret"] = channel_cfg.get("app_secret", "")
        cfg["allowed_open_ids"] = channel_cfg.get("allowed_open_ids", [])
        cfg["auto_respond_chats"] = channel_cfg.get("auto_respond_chats", [])
        cfg["log_chat_id"] = channel_cfg.get("log_chat_id", "")
    elif active_channel == "dingtalk":
        cfg["app_id"] = channel_cfg.get("client_id", "")
        cfg["app_secret"] = channel_cfg.get("client_secret", "")

    log.info(f"Loaded config for '{bot_name}' from Firestore (channel={active_channel})")
    return cfg