#!/usr/bin/env python3
"""Send a message to another bot via Firestore Inbox.

Usage:
    python3 ~/CloseCrab/scripts/inbox-send.py <target_bot> "<message>"

Environment:
    BOT_NAME: sender bot name (auto-set by main.py)

Examples:
    python3 ~/CloseCrab/scripts/inbox-send.py jarvis "Tommy 报到，一切正常"
    python3 ~/CloseCrab/scripts/inbox-send.py hulk "请帮忙查一下 GPU 状态"
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main():
    if len(sys.argv) < 3:
        print("Usage: inbox-send.py <target_bot> <message>")
        sys.exit(1)

    target = sys.argv[1]
    message = sys.argv[2]
    sender = os.environ.get("BOT_NAME", "unknown")

    from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
    from google.cloud import firestore
    from datetime import datetime, timezone

    db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
    doc_data = {
        "from": sender,
        "to": target,
        "instruction": message,
        "task_id": "",
        "status": "pending",
        "result": "",
        "created_at": datetime.now(timezone.utc),
    }
    _, ref = db.collection("messages").add(doc_data)
    print(f"Sent to {target}: {message[:60]} (doc_id={ref.id})")

if __name__ == "__main__":
    main()
