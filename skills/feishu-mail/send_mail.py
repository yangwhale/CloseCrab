#!/usr/bin/env python3
"""Send email via Feishu enterprise SMTP."""

import argparse
import os
import smtplib
import sys
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
            email = (doc.to_dict() or {}).get("email", {})
            if email:
                return email
    except Exception:
        pass
    return {}

_email_cfg = _load_email_config()
SMTP_HOST = _email_cfg.get("smtp_host") or os.environ.get("FEISHU_SMTP_HOST", "smtp.feishu.cn")
SMTP_PORT = int(_email_cfg.get("smtp_port") or os.environ.get("FEISHU_SMTP_PORT", "465"))
SMTP_USER = _email_cfg.get("user") or os.environ.get("FEISHU_SMTP_USER", "")
SMTP_PASS = _email_cfg.get("pass") or os.environ.get("FEISHU_SMTP_PASS", "")


def send_mail(to: list[str], subject: str, body: str, cc: list[str] = None, html: bool = False):
    if not SMTP_PASS:
        print("ERROR: FEISHU_SMTP_PASS not set", file=sys.stderr)
        sys.exit(1)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    sender_name = os.environ.get("BOT_NAME", SMTP_USER.split("@")[0]).title()
    msg["From"] = f"{sender_name} <{SMTP_USER}>"
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)

    content_type = "html" if html else "plain"
    msg.attach(MIMEText(body, content_type, "utf-8"))

    recipients = list(to) + (cc or [])

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg, to_addrs=recipients)

    print(f"OK: sent to {', '.join(recipients)}")


def main():
    parser = argparse.ArgumentParser(description="Send email via Feishu SMTP")
    parser.add_argument("--to", required=True, help="Recipients (comma-separated)")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", required=True, help="Email body")
    parser.add_argument("--cc", default="", help="CC recipients (comma-separated)")
    parser.add_argument("--html", action="store_true", help="Treat body as HTML")
    args = parser.parse_args()

    to = [a.strip() for a in args.to.split(",") if a.strip()]
    cc = [a.strip() for a in args.cc.split(",") if a.strip()] if args.cc else None

    send_mail(to, args.subject, args.body, cc, args.html)


if __name__ == "__main__":
    main()
