#!/usr/bin/env python3
"""以 Chris 身份通过飞书 API 发消息到 bot DM 窗口。"""
import argparse, json, os, sys, time
import requests

TOKEN_FILE = os.path.expanduser("~/.closecrab/feishu-user-token.json")
OAUTH_APP = "jarvis"  # 用 jarvis 的 app 做 OAuth

def _firestore_feishu_config(bot_name: str) -> dict:
    from closecrab.constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
    from google.cloud.firestore import Client
    db = Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
    doc = db.document(f"bots/{bot_name}").get()
    return doc.to_dict().get("channels", {}).get("feishu", {})

def _get_tenant_token() -> str:
    cfg = _firestore_feishu_config(OAUTH_APP)
    resp = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                         json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]}, timeout=10)
    resp.raise_for_status()
    return resp.json()["tenant_access_token"]

def load_token() -> dict:
    if not os.path.exists(TOKEN_FILE):
        return {}
    with open(TOKEN_FILE) as f:
        return json.load(f)

def save_token(data: dict):
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)

def refresh_if_needed(data: dict) -> dict:
    expires_at = data.get("expires_at", 0)
    if time.time() < expires_at - 60:
        return data
    print("Token expired, refreshing...", file=sys.stderr)
    tenant_token = _get_tenant_token()
    resp = requests.post("https://open.feishu.cn/open-apis/authen/v1/oidc/refresh_access_token",
                         headers={"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"},
                         json={"grant_type": "refresh_token", "refresh_token": data["refresh_token"]}, timeout=10)
    resp.raise_for_status()
    r = resp.json()
    if r.get("code") != 0:
        print(f"Refresh failed: {r}", file=sys.stderr)
        sys.exit(1)
    d = r["data"]
    data["access_token"] = d["access_token"]
    data["refresh_token"] = d["refresh_token"]
    data["expires_at"] = time.time() + d.get("expires_in", 7200)
    data["refresh_expires_at"] = time.time() + d.get("refresh_expires_in", 2592000)
    save_token(data)
    print("Token refreshed OK", file=sys.stderr)
    days_left = (data["refresh_expires_at"] - time.time()) / 86400
    if days_left < 7:
        print(f"⚠️  Refresh token expires in {days_left:.0f} days! Re-authorize soon.", file=sys.stderr)
    return data

def _get_bot_tenant_token(bot_name: str) -> str:
    cfg = _firestore_feishu_config(bot_name)
    resp = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                         json={"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]}, timeout=10)
    resp.raise_for_status()
    return resp.json()["tenant_access_token"]

CHRIS_OPEN_ID = "ou_574002d3a6d7cec10d3c45e8d3e4eda4"

def resolve_chat_id(data: dict, bot_name: str) -> str:
    cached = data.get("chat_ids", {}).get(bot_name)
    if cached:
        return cached
    # Fallback: 从本地 user_chats.json 查
    uc_path = os.path.expanduser(f"~/.claude/closecrab/{bot_name}/user_chats.json")
    if os.path.exists(uc_path):
        import json as _j
        with open(uc_path) as _f:
            uc = _j.load(_f)
        for uid, cid in uc.items():
            data.setdefault("chat_ids", {})[bot_name] = cid
            save_token(data)
            print(f"Resolved {bot_name} → {cid} (from user_chats.json)", file=sys.stderr)
            return cid
    bot_token = _get_bot_tenant_token(bot_name)
    page_token = ""
    while True:
        params = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        resp = requests.get("https://open.feishu.cn/open-apis/im/v1/chats",
                            headers={"Authorization": f"Bearer {bot_token}"}, params=params, timeout=10)
        r = resp.json()
        if r.get("code") != 0:
            print(f"List chats failed for {bot_name}: {r.get('msg')}", file=sys.stderr)
            break
        for item in r.get("data", {}).get("items", []):
            if item.get("chat_mode") == "p2p":
                chat_id = item["chat_id"]
                members_resp = requests.get(
                    f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members",
                    headers={"Authorization": f"Bearer {bot_token}"},
                    params={"member_id_type": "open_id", "page_size": 10}, timeout=10)
                mr = members_resp.json()
                member_ids = [m.get("member_id") for m in mr.get("data", {}).get("items", [])]
                if CHRIS_OPEN_ID in member_ids:
                    data.setdefault("chat_ids", {})[bot_name] = chat_id
                    save_token(data)
                    print(f"Resolved {bot_name} → {chat_id}", file=sys.stderr)
                    return chat_id
        if not r.get("data", {}).get("has_more"):
            break
        page_token = r["data"].get("page_token", "")
    # Fallback: 用 bot tenant token 主动创建/获取 p2p chat
    resp = requests.post("https://open.feishu.cn/open-apis/im/v1/chats?set_bot_manager=false",
                         headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json"},
                         json={"chat_mode": "p2p", "user_id_list": [CHRIS_OPEN_ID], "user_id_type": "open_id"},
                         timeout=10)
    r = resp.json()
    if r.get("code") == 0:
        chat_id = r["data"]["chat_id"]
        data.setdefault("chat_ids", {})[bot_name] = chat_id
        save_token(data)
        print(f"Created/found p2p chat {bot_name} → {chat_id}", file=sys.stderr)
        return chat_id
    print(f"Could not resolve chat with {bot_name}: {r.get('msg')}", file=sys.stderr)
    sys.exit(1)

def send_text(token: str, chat_id: str, text: str):
    resp = requests.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"receive_id": chat_id, "msg_type": "text",
                               "content": json.dumps({"text": text})}, timeout=10)
    r = resp.json()
    if r.get("code") != 0:
        print(f"Send failed: {r}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Sent to {chat_id}: {text[:60]}")

def send_audio(token: str, chat_id: str, audio_path: str):
    with open(audio_path, "rb") as f:
        up = requests.post("https://open.feishu.cn/open-apis/im/v1/files",
                           headers={"Authorization": f"Bearer {token}"},
                           data={"file_type": "opus", "file_name": os.path.basename(audio_path)},
                           files={"file": (os.path.basename(audio_path), f, "audio/ogg")}, timeout=30)
    ur = up.json()
    if ur.get("code") != 0:
        print(f"Upload failed: {ur}", file=sys.stderr)
        sys.exit(1)
    file_key = ur["data"]["file_key"]
    resp = requests.post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                         json={"receive_id": chat_id, "msg_type": "audio",
                               "content": json.dumps({"file_key": file_key})}, timeout=10)
    r = resp.json()
    if r.get("code") != 0:
        print(f"Send audio failed: {r}", file=sys.stderr)
        sys.exit(1)
    print(f"✅ Sent audio to {chat_id}: {audio_path}")

def init_token(code: str):
    tenant_token = _get_tenant_token()
    resp = requests.post("https://open.feishu.cn/open-apis/authen/v1/oidc/access_token",
                         headers={"Authorization": f"Bearer {tenant_token}", "Content-Type": "application/json"},
                         json={"grant_type": "authorization_code", "code": code}, timeout=10)
    r = resp.json()
    if r.get("code") != 0:
        print(f"Token exchange failed: {r}", file=sys.stderr)
        sys.exit(1)
    d = r["data"]
    data = {
        "access_token": d["access_token"],
        "refresh_token": d["refresh_token"],
        "expires_at": time.time() + d.get("expires_in", 7200),
        "refresh_expires_at": time.time() + d.get("refresh_expires_in", 2592000),
        "scope": d.get("scope", ""),
        "chat_ids": {},
    }
    save_token(data)
    print(f"✅ Token initialized. Scope: {data['scope']}")
    print(f"   Access token expires: {time.strftime('%Y-%m-%d %H:%M', time.localtime(data['expires_at']))}")
    print(f"   Refresh token expires: {time.strftime('%Y-%m-%d %H:%M', time.localtime(data['refresh_expires_at']))}")

def show_status():
    data = load_token()
    if not data:
        print("❌ No token found. Run --init-token first.")
        return
    now = time.time()
    at_ok = now < data.get("expires_at", 0)
    rt_days = (data.get("refresh_expires_at", 0) - now) / 86400
    print(f"Access token:  {'✅ valid' if at_ok else '⚠️  expired (will auto-refresh)'}")
    print(f"Refresh token: {'✅' if rt_days > 0 else '❌ expired'} ({rt_days:.0f} days left)")
    print(f"Scope: {data.get('scope', 'N/A')}")
    cached = data.get("chat_ids", {})
    if cached:
        print(f"Cached chat IDs: {', '.join(cached.keys())}")
    else:
        print("No cached chat IDs yet (will resolve on first send)")

def main():
    p = argparse.ArgumentParser(description="Send Feishu message as Chris")
    p.add_argument("--to", help="Target bot name (jarvis/bunny/xiaoaitongxue/tiemu/tommy/hulk/tianmaojingling)")
    p.add_argument("--text", help="Text message to send")
    p.add_argument("--audio", help="Audio file path to send as voice message")
    p.add_argument("--init-token", action="store_true", help="Initialize token from OAuth code")
    p.add_argument("--code", help="OAuth authorization code (used with --init-token)")
    p.add_argument("--status", action="store_true", help="Show token status")
    args = p.parse_args()

    if args.status:
        show_status()
        return
    if args.init_token:
        if not args.code:
            cfg = _firestore_feishu_config(OAUTH_APP)
            print(f"Open this URL to authorize, then pass the code with --code:")
            print(f"https://open.feishu.cn/open-apis/authen/v1/authorize?app_id={cfg['app_id']}"
                  f"&redirect_uri=https%3A//cc.higcp.com/oauth/callback.html"
                  f"&response_type=code&scope=im:message%20im:message.send_as_user")
            return
        init_token(args.code)
        return
    if not args.to or (not args.text and not args.audio):
        p.print_help()
        return

    data = load_token()
    if not data:
        print("❌ No token. Run --init-token first.", file=sys.stderr)
        sys.exit(1)
    data = refresh_if_needed(data)
    chat_id = resolve_chat_id(data, args.to)

    if args.text:
        send_text(data["access_token"], chat_id, args.text)
    if args.audio:
        send_audio(data["access_token"], chat_id, args.audio)

if __name__ == "__main__":
    main()
