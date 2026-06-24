---
name: wechat-reader
description: Read WeChat Official Account (微信公众号) articles. Trigger when user shares any mp.weixin.qq.com URL, or says "微信文章", "公众号文章", "读一下这个微信链接", "WeChat article". Bypasses WeChat's anti-scraping CAPTCHA by emulating the WeChat in-app browser. MUST be used instead of WebFetch/Jina for any mp.weixin.qq.com link — those tools WILL fail with "环境异常" CAPTCHA.
---

# WeChat Article Reader

Read 微信公众号 articles that are normally blocked by WeChat's slider CAPTCHA verification.

## When to Use

**Always use this skill when you see a `mp.weixin.qq.com` URL.** Regular web fetch tools (WebFetch, Jina read_url, fetch_resource) CANNOT read WeChat articles — they get blocked by the "环境异常" slider CAPTCHA. This skill is the ONLY way to read WeChat article content.

Trigger keywords: `mp.weixin.qq.com`, `微信`, `公众号`, `WeChat article`, `微信文章`, `公众号文章`

## How It Works

WeChat blocks non-WeChat browsers with a slider CAPTCHA ("环境异常"). The bypass: set User-Agent to include `MicroMessenger` — WeChat's server treats the request as coming from the WeChat app and serves the article directly.

## Usage

When a user shares a `mp.weixin.qq.com/s/...` URL, run the reader script:

```bash
python3 ~/CloseCrab/skills/wechat-reader/scripts/read_wechat.py "<URL>"
```

The script outputs JSON with `title`, `author`, `date`, and `content` fields.

### Requirements

- Playwright with Chromium installed (`playwright install chromium`)
- Python package: `playwright`

If Playwright is not available, fall back to curl with WeChat User-Agent:

```bash
curl -s -H "User-Agent: Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 MicroMessenger/8.0.50.2701 WeChat/arm64" "<URL>"
```

## Notes

- The UA string **must** contain `MicroMessenger` — this is the only required token
- Simple HTTP requests (curl, WebFetch, Jina Reader) without the WeChat UA cannot bypass the CAPTCHA
- WeChat may change their detection logic; if this stops working, update the UA string to match a recent WeChat Android version
