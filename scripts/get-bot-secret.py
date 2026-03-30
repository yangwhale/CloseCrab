#!/usr/bin/env python3
"""Read a bot config field from Firestore.

Usage:
    get-bot-secret.py <bot_name> <field_path>

Examples:
    get-bot-secret.py jarvis channels.discord.token
    get-bot-secret.py hulk channels.feishu.app_id
    get-bot-secret.py jarvis email.user

Output: prints the value to stdout (no trailing newline for shell capture).
Exit code 1 if bot or field not found.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <bot_name> <field_path>", file=sys.stderr)
        sys.exit(1)

    bot_name = sys.argv[1]
    field_path = sys.argv[2]

    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
    doc = db.collection("bots").document(bot_name).get()

    if not doc.exists:
        print(f"Bot '{bot_name}' not found in Firestore", file=sys.stderr)
        sys.exit(1)

    data = doc.to_dict() or {}

    # Navigate nested path: "channels.discord.token" -> data["channels"]["discord"]["token"]
    value = data
    for key in field_path.split("."):
        if isinstance(value, dict):
            value = value.get(key)
        else:
            value = None
            break

    if value is None:
        print(f"Field '{field_path}' not found for bot '{bot_name}'", file=sys.stderr)
        sys.exit(1)

    print(value, end="")


if __name__ == "__main__":
    main()
