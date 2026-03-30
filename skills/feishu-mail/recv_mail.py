#!/usr/bin/env python3
"""Read emails from Feishu enterprise IMAP."""

import argparse
import email
import email.message
import imaplib
import json
import os
import re
import sys
from email.header import decode_header
from pathlib import Path

# Load email config from Firestore (fallback to env vars)
def _load_email_config():
    bot = os.environ.get("BOT_NAME", "jarvis")
    try:
        from google.cloud import firestore
        project = os.environ.get("FIRESTORE_PROJECT", "")
        database = os.environ.get("FIRESTORE_DATABASE", "closecrab")
        db = firestore.Client(project=project, database=database)
        doc = db.collection("bots").document(bot).get()
        if doc.exists:
            email_cfg = (doc.to_dict() or {}).get("email", {})
            if email_cfg:
                return email_cfg
    except Exception:
        pass
    return {}

_email_cfg = _load_email_config()
IMAP_HOST = _email_cfg.get("imap_host") or os.environ.get("FEISHU_IMAP_HOST", "imap.feishu.cn")
IMAP_PORT = int(_email_cfg.get("imap_port") or os.environ.get("FEISHU_IMAP_PORT", "993"))
IMAP_USER = _email_cfg.get("user") or os.environ.get("FEISHU_SMTP_USER", "")
IMAP_PASS = _email_cfg.get("pass") or os.environ.get("FEISHU_SMTP_PASS", "")


def _decode_header(raw: str) -> str:
    if not raw:
        return ""
    parts = []
    for part, enc in decode_header(raw):
        if isinstance(part, bytes):
            parts.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(part)
    return "".join(parts)


def _extract_text(msg: email.message.Message) -> str:
    """Extract plain text from email, fallback to stripped HTML."""
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        if payload:
            text = payload.decode("utf-8", errors="replace")
            if msg.get_content_type() == "text/html":
                text = re.sub(r"<[^>]+>", "", text)
                text = re.sub(r"\s+", " ", text).strip()
            return text
        return ""

    plain = ""
    html = ""
    for part in msg.walk():
        ct = part.get_content_type()
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        decoded = payload.decode("utf-8", errors="replace")
        if ct == "text/plain" and not plain:
            plain = decoded
        elif ct == "text/html" and not html:
            html = decoded

    if plain:
        return plain
    if html:
        text = re.sub(r"<[^>]+>", "", html)
        return re.sub(r"\s+", " ", text).strip()
    return ""


def list_mail(folder: str = "INBOX", limit: int = 10, unread_only: bool = False,
              search_from: str = None, search_subject: str = None, output_json: bool = False):
    if not IMAP_PASS:
        print("ERROR: FEISHU_SMTP_PASS not set", file=sys.stderr)
        sys.exit(1)

    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    m.login(IMAP_USER, IMAP_PASS)
    m.select(folder, readonly=True)

    # Build search criteria
    criteria = []
    if unread_only:
        criteria.append("UNSEEN")
    if search_from:
        criteria.append(f'FROM "{search_from}"')
    if search_subject:
        criteria.append(f'SUBJECT "{search_subject}"')
    if not criteria:
        criteria.append("ALL")

    status, data = m.search(None, " ".join(criteria))
    ids = data[0].split()

    if not ids:
        if output_json:
            print(json.dumps([], ensure_ascii=False))
        else:
            print("No emails found.")
        m.logout()
        return

    # Take last N (most recent)
    selected = ids[-limit:]
    results = []

    for eid in selected:
        status, msg_data = m.fetch(eid, "(RFC822 FLAGS)")
        msg = email.message_from_bytes(msg_data[0][1])

        # Parse flags
        flags_raw = msg_data[0][0].decode() if msg_data[0][0] else ""
        is_read = "\\Seen" in flags_raw

        subject = _decode_header(msg["Subject"])
        from_addr = _decode_header(msg["From"])
        to_addr = _decode_header(msg["To"])
        date = msg["Date"]
        message_id = msg["Message-ID"]
        body = _extract_text(msg)

        entry = {
            "id": eid.decode(),
            "from": from_addr,
            "to": to_addr,
            "subject": subject,
            "date": date,
            "read": is_read,
            "message_id": message_id,
            "body": body[:500],
        }
        results.append(entry)

    m.logout()

    if output_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for e in results:
            status_mark = " " if e["read"] else "*"
            print(f"[{status_mark}] {e['id']} | {e['date']}")
            print(f"    From: {e['from']}")
            print(f"    Subject: {e['subject']}")
            print(f"    Body: {e['body'][:150]}")
            print()


def main():
    parser = argparse.ArgumentParser(description="Read emails from Feishu IMAP")
    parser.add_argument("--folder", default="INBOX", help="Mailbox folder (default: INBOX)")
    parser.add_argument("--limit", type=int, default=10, help="Max emails to fetch (default: 10)")
    parser.add_argument("--unread", action="store_true", help="Only show unread emails")
    parser.add_argument("--from", dest="search_from", default=None, help="Filter by sender")
    parser.add_argument("--subject", default=None, help="Filter by subject")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    list_mail(args.folder, args.limit, args.unread, args.search_from, args.subject, args.json)


if __name__ == "__main__":
    main()
