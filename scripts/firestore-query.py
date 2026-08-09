#!/usr/bin/env python3
"""Firestore query helper for launcher.sh.

Usage:
    firestore-query.py bot-exists <bot_name>       # exit 0 if exists, 1 if not
    firestore-query.py all-bots                    # print all bot names, one per line
    firestore-query.py registry <bot_name> <field> # print a registry field (hostname, status, ip, ...)
    firestore-query.py registry-set <bot_name> <field> <value>  # update a registry field
    firestore-query.py bots-on-host <bot_name>     # print other online bots on the same host
    firestore-query.py status                      # print all bots status table
"""

import sys
from pathlib import Path

# Add project root to sys.path for closecrab.constants
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
from google.cloud import firestore


def get_db():
    return firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)


def cmd_bot_exists(args):
    if len(args) != 1:
        print("Usage: firestore-query.py bot-exists <bot_name>", file=sys.stderr)
        sys.exit(2)
    db = get_db()
    doc = db.collection("bots").document(args[0]).get()
    sys.exit(0 if doc.exists else 1)


def cmd_all_bots(args):
    db = get_db()
    for doc in db.collection("bots").stream():
        print(doc.id)


def cmd_registry(args):
    if len(args) < 1:
        print("Usage: firestore-query.py registry <bot_name> [field]", file=sys.stderr)
        sys.exit(2)
    db = get_db()
    doc = db.collection("registry").document(args[0]).get()
    if not doc.exists:
        sys.exit(1)
    data = doc.to_dict() or {}
    if len(args) >= 2:
        val = data.get(args[1], "")
        print(val, end="")
    else:
        for k, v in sorted(data.items()):
            print(f"{k}={v}")


def cmd_registry_set(args):
    if len(args) != 3:
        print("Usage: firestore-query.py registry-set <bot_name> <field> <value>", file=sys.stderr)
        sys.exit(2)
    db = get_db()
    db.collection("registry").document(args[0]).set({args[1]: args[2]}, merge=True)


def cmd_bots_on_host(args):
    """Print bot names on the same hostname as the given bot (excluding itself)."""
    if len(args) != 1:
        print("Usage: firestore-query.py bots-on-host <bot_name>", file=sys.stderr)
        sys.exit(2)
    bot_name = args[0]
    db = get_db()
    doc = db.collection("registry").document(bot_name).get()
    if not doc.exists:
        sys.exit(0)
    hostname = (doc.to_dict() or {}).get("hostname", "")
    if not hostname:
        sys.exit(0)
    for reg_doc in db.collection("registry").stream():
        if reg_doc.id == bot_name:
            continue
        data = reg_doc.to_dict() or {}
        if data.get("hostname") == hostname and data.get("status") == "online":
            print(reg_doc.id)


def cmd_status(args):
    db = get_db()
    bots = {doc.id for doc in db.collection("bots").stream()}
    registry = {}
    for doc in db.collection("registry").stream():
        registry[doc.id] = doc.to_dict() or {}

    import socket
    my_hostname = socket.gethostname()

    for name in sorted(bots):
        reg = registry.get(name, {})
        status = reg.get("status", "offline")
        hostname = reg.get("hostname", "-")
        last_seen = reg.get("last_seen", "-")
        channel = reg.get("channel", "-")
        # Check if this bot is on current machine
        here = ""
        if hostname and (hostname == my_hostname or hostname.startswith(my_hostname.split(".")[0])):
            here = " <- here"
        icon = "O" if status == "online" else "x"
        print(f"  {icon} {name:18s} host={hostname:45s} status={status:8s} channel={channel:8s} last_seen={last_seen}{here}")


def main():
    if len(sys.argv) < 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    commands = {
        "bot-exists": cmd_bot_exists,
        "all-bots": cmd_all_bots,
        "bots-on-host": cmd_bots_on_host,
        "registry": cmd_registry,
        "registry-set": cmd_registry_set,
        "status": cmd_status,
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(2)

    commands[cmd](args)


if __name__ == "__main__":
    main()
