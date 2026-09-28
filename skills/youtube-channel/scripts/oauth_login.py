#!/usr/bin/env python3
"""一次性 OAuth 授权：用本机已登录频道账号的 Chrome 打开打印出的 URL 点同意，回调落在本机 18765 端口。
前置：~/.config/youtube/client_secret.json（GCP 项目里建的「桌面应用」OAuth 客户端）。产物 token.json（600）。
OAuth 应用处于 Testing 状态时 refresh token 7 天过期，过期就重跑本脚本。"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow
D = os.path.expanduser("~/.config/youtube/")
SCOPES = ["https://www.googleapis.com/auth/youtube.upload", "https://www.googleapis.com/auth/youtube.force-ssl"]
flow = InstalledAppFlow.from_client_secrets_file(D + "client_secret.json", SCOPES)
creds = flow.run_local_server(port=18765, open_browser=False, authorization_prompt_message="URL: {url}", prompt="consent", access_type="offline")
open(D + "token.json", "w").write(creds.to_json()); os.chmod(D + "token.json", 0o600)
print("TOKEN_SAVED", flush=True)
