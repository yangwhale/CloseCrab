---
name: wechat-reader
description: Read WeChat Official Account (微信公众号) articles from mp.weixin.qq.com URLs. Use when a user shares a WeChat article link (mp.weixin.qq.com/s/...) and wants to read, summarize, or analyze the content. Bypasses WeChat's anti-scraping CAPTCHA by emulating the WeChat in-app browser.
---

# WeChat Article Reader

Read 微信公众号 articles that are normally blocked by WeChat's slider CAPTCHA verification.

## How It Works

WeChat blocks non-WeChat browsers with a slider CAPTCHA ("环境异常"). The bypass: set User-Agent to include `MicroMessenger` — WeChat's server treats the request as coming from the WeChat app and serves the article directly.

## Usage

When a user shares a `mp.weixin.qq.com/s/...` URL, run the reader script:

```bash
python3 skills/wechat-reader/scripts/read_wechat.py "<URL>"
```

The script outputs JSON with `title`, `author`, `date`, and `content` fields.

### Requirements

- Playwright with Chromium installed (`playwright install chromium`)
- Python package: `playwright`

If Playwright is not available, fall back to the MCP approach below.

## Fallback: Playwright MCP

If the script fails or Playwright is not installed as a CLI tool, use the Playwright MCP browser directly:

```javascript
// 1. Create context with WeChat UA
const context = await page.context().browser().newContext({
  userAgent: '...MicroMessenger/8.0.50.2701(0x28003253) WeChat/arm64 Weixin...',
  viewport: { width: 390, height: 844 }
});

// 2. Navigate and extract
const newPage = await context.newPage();
await newPage.goto(url, { waitUntil: 'domcontentloaded' });
const result = await newPage.evaluate(() => ({
  title: document.querySelector('#activity-name')?.innerText?.trim(),
  author: document.querySelector('#js_name')?.innerText?.trim(),
  date: document.querySelector('#publish_time')?.innerText?.trim(),
  content: document.querySelector('#js_content')?.innerText
}));
```

Key selectors: `#activity-name` (title), `#js_name` (author), `#publish_time` (date), `#js_content` (body).

## Notes

- The UA string **must** contain `MicroMessenger` — this is the only required token
- Simple HTTP requests (curl, WebFetch, Jina Reader) cannot bypass the CAPTCHA — a real browser engine is needed
- WeChat may change their detection logic; if this stops working, update the UA string to match a recent WeChat Android version
