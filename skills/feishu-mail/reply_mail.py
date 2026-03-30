#!/usr/bin/env python3
"""Reply to an email via Feishu enterprise SMTP."""

import argparse
import email
import imaplib
import os
import smtplib
import sys
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
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
SMTP_HOST = _email_cfg.get("smtp_host") or os.environ.get("FEISHU_SMTP_HOST", "smtp.feishu.cn")
SMTP_PORT = int(_email_cfg.get("smtp_port") or os.environ.get("FEISHU_SMTP_PORT", "465"))
IMAP_HOST = _email_cfg.get("imap_host") or os.environ.get("FEISHU_IMAP_HOST", "imap.feishu.cn")
IMAP_PORT = int(_email_cfg.get("imap_port") or os.environ.get("FEISHU_IMAP_PORT", "993"))
MAIL_USER = _email_cfg.get("user") or os.environ.get("FEISHU_SMTP_USER", "")
MAIL_PASS = _email_cfg.get("pass") or os.environ.get("FEISHU_SMTP_PASS", "")


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


def reply_mail(email_id: str, body: str, html: bool = False, folder: str = "INBOX"):
    if not MAIL_PASS:
        print("ERROR: FEISHU_SMTP_PASS not set", file=sys.stderr)
        sys.exit(1)

    # Fetch original email to get headers for threading
    m = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    m.login(MAIL_USER, MAIL_PASS)
    m.select(folder, readonly=True)

    status, msg_data = m.fetch(email_id.encode(), "(RFC822)")
    if status != "OK":
        print(f"ERROR: email {email_id} not found", file=sys.stderr)
        m.logout()
        sys.exit(1)

    orig = email.message_from_bytes(msg_data[0][1])
    m.logout()

    orig_from = _decode_header(orig["From"])
    orig_subject = _decode_header(orig["Subject"])
    orig_message_id = orig["Message-ID"]
    orig_references = orig.get("References", "")

    # Extract reply-to address (prefer Reply-To header, fallback to From)
    reply_to = orig.get("Reply-To") or orig["From"]
    # Extract just the email address
    import re
    addr_match = re.search(r"<([^>]+)>", reply_to)
    reply_addr = addr_match.group(1) if addr_match else reply_to.strip()

    # Build reply
    reply = MIMEMultipart("alternative")
    reply["Subject"] = f"Re: {orig_subject}" if not orig_subject.startswith("Re:") else orig_subject
    reply["From"] = f"Jarvis <{MAIL_USER}>"
    reply["To"] = reply_addr
    reply["In-Reply-To"] = orig_message_id
    reply["References"] = f"{orig_references} {orig_message_id}".strip()

    content_type = "html" if html else "plain"
    reply.attach(MIMEText(body, content_type, "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(MAIL_USER, MAIL_PASS)
        s.send_message(reply)

    print(f"OK: replied to {reply_addr} (subject: {reply['Subject']})")


def main():
    parser = argparse.ArgumentParser(description="Reply to an email via Feishu SMTP")
    parser.add_argument("--id", required=True, help="Email ID from recv_mail.py output")
    parser.add_argument("--body", required=True, help="Reply body text")
    parser.add_argument("--html", action="store_true", help="Treat body as HTML")
    parser.add_argument("--folder", default="INBOX", help="Mailbox folder (default: INBOX)")
    args = parser.parse_args()

    reply_mail(args.id, args.body, args.html, args.folder)


if __name__ == "__main__":
    main()
