#!/usr/bin/env python3
"""Create Feishu public mailbox and configure SMTP credentials in Firestore.

Usage:
  # Step 1: Create public mailbox
  python3 setup_mailbox.py create --email bot@domain.com --name "Bot Name"

  # Step 2: After enabling SMTP in admin console, set password
  python3 setup_mailbox.py set-password --bot athena --password "xxx" [--database closecrab-public]

  # List existing public mailboxes
  python3 setup_mailbox.py list

  # Delete a public mailbox
  python3 setup_mailbox.py delete --mailbox-id "1RNM10VNQREQPKK"
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error


def _get_firestore_client(database=None):
    from google.cloud import firestore
    project = os.environ.get("FIRESTORE_PROJECT")
    db_name = database or os.environ.get("FIRESTORE_DATABASE", "closecrab")
    return firestore.Client(project=project, database=db_name)


def _get_feishu_token(db=None):
    """Get tenant_access_token using Jarvis's feishu app credentials."""
    if db is None:
        db = _get_firestore_client("closecrab")

    # Try to find a bot with feishu channel config that has mail:public_mailbox
    # Default: jarvis (known to have the permission)
    admin_bot = os.environ.get("MAIL_ADMIN_BOT", "jarvis")
    doc = db.collection("bots").document(admin_bot).get()
    if not doc.exists:
        print(f"ERROR: bot '{admin_bot}' not found in Firestore", file=sys.stderr)
        sys.exit(1)

    cfg = doc.to_dict()
    feishu_cfg = (cfg.get("channels") or {}).get("feishu") or {}
    app_id = feishu_cfg.get("app_id")
    app_secret = feishu_cfg.get("app_secret")

    if not app_id or not app_secret:
        print(f"ERROR: bot '{admin_bot}' has no feishu channel config", file=sys.stderr)
        sys.exit(1)

    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req)
    result = json.loads(resp.read())
    if result.get("code") != 0:
        print(f"ERROR: token request failed: {result}", file=sys.stderr)
        sys.exit(1)
    return result["tenant_access_token"]


def _feishu_api(method, path, token, body=None):
    """Make a Feishu API request."""
    url = f"https://open.feishu.cn/open-apis{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        try:
            return json.loads(body_text)
        except json.JSONDecodeError:
            print(f"HTTP {e.code}: {body_text}", file=sys.stderr)
            sys.exit(1)


def cmd_create(args):
    """Create a public mailbox via Feishu API."""
    token = _get_feishu_token()
    result = _feishu_api("POST", "/mail/v1/public_mailboxes", token, {
        "email": args.email,
        "name": args.name,
    })

    if result.get("code") == 0:
        data = result["data"]
        print(f"OK: created {data['email']}")
        print(f"  mailbox_id: {data['public_mailbox_id']}")
        print(f"  name: {data.get('name', '')}")
        print()
        print("Next steps:")
        print(f"  1. Go to Feishu Admin → Email → Public Mailboxes")
        print(f"     https://your-org.feishu.cn/admin/email/public_mailbox")
        print(f"  2. Find '{args.email}' → Enable SMTP → Generate app password")
        print(f"  3. Run: python3 {__file__} set-password --bot <bot_name> --password <password>")
    else:
        print(f"ERROR: {result.get('msg', result)}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args):
    """List all public mailboxes."""
    token = _get_feishu_token()
    result = _feishu_api("GET", "/mail/v1/public_mailboxes?page_size=50", token)

    if result.get("code") == 0:
        items = result.get("data", {}).get("items", [])
        if not items:
            print("No public mailboxes found.")
            return
        for item in items:
            print(f"  {item.get('email', '?'):30s}  name={item.get('name', '')}  id={item.get('public_mailbox_id', '')}")
    else:
        print(f"ERROR: {result.get('msg', result)}", file=sys.stderr)
        sys.exit(1)


def cmd_delete(args):
    """Delete a public mailbox."""
    token = _get_feishu_token()
    result = _feishu_api("DELETE", f"/mail/v1/public_mailboxes/{args.mailbox_id}", token)

    if result.get("code") == 0:
        print(f"OK: deleted mailbox {args.mailbox_id}")
    else:
        print(f"ERROR: {result.get('msg', result)}", file=sys.stderr)
        sys.exit(1)


def cmd_set_password(args):
    """Set SMTP password for a bot's email config in Firestore."""
    db = _get_firestore_client(args.database)
    doc_ref = db.collection("bots").document(args.bot)
    doc = doc_ref.get()

    if not doc.exists:
        print(f"ERROR: bot '{args.bot}' not found in database '{args.database}'", file=sys.stderr)
        sys.exit(1)

    cfg = doc.to_dict()
    email_cfg = cfg.get("email") or {}

    if not email_cfg.get("user"):
        # If email config doesn't exist yet, create a full one
        email_cfg = {
            "smtp_host": "smtp.feishu.cn",
            "smtp_port": 465,
            "imap_host": "imap.feishu.cn",
            "imap_port": 993,
            "user": args.email or f"{args.bot}@your-domain.com",
            "pass": args.password,
        }
        doc_ref.update({"email": email_cfg})
        print(f"OK: created email config for '{args.bot}' ({email_cfg['user']})")
    else:
        doc_ref.update({"email.pass": args.password})
        print(f"OK: updated SMTP password for '{args.bot}' ({email_cfg['user']})")


def main():
    parser = argparse.ArgumentParser(description="Feishu public mailbox management")
    sub = parser.add_subparsers(dest="command", required=True)

    # create
    p_create = sub.add_parser("create", help="Create a public mailbox")
    p_create.add_argument("--email", required=True, help="Email address (e.g. bot@your-domain.com)")
    p_create.add_argument("--name", required=True, help="Display name")

    # list
    sub.add_parser("list", help="List all public mailboxes")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a public mailbox")
    p_delete.add_argument("--mailbox-id", required=True, help="Public mailbox ID")

    # set-password
    p_pass = sub.add_parser("set-password", help="Set SMTP password in Firestore")
    p_pass.add_argument("--bot", required=True, help="Bot name in Firestore")
    p_pass.add_argument("--password", required=True, help="SMTP/IMAP password")
    p_pass.add_argument("--email", default="", help="Email address (if creating new config)")
    p_pass.add_argument("--database", default=None,
                        help="Firestore database (default: FIRESTORE_DATABASE or closecrab)")

    args = parser.parse_args()

    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "delete": cmd_delete,
        "set-password": cmd_set_password,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
