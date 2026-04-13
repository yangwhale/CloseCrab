#!/usr/bin/env python3
"""Read WeChat Official Account articles by emulating WeChat in-app browser."""

import json
import sys

WECHAT_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/126.0.6478.71 Mobile Safari/537.36 "
    "XWEB/1260117 MMWEBSDK/20240501 MMWEBID/1234 "
    "MicroMessenger/8.0.50.2701(0x28003253) "
    "WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64"
)


def read_article(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=WECHAT_UA,
            viewport={"width": 390, "height": 844},
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=20000)

        result = page.evaluate("""() => {
            const q = (sel) => document.querySelector(sel);
            return {
                title: q('#activity-name')?.innerText?.trim() || q('.rich_media_title')?.innerText?.trim() || '',
                author: q('#js_name')?.innerText?.trim() || q('.rich_media_meta_nickname')?.innerText?.trim() || '',
                date: q('#publish_time')?.innerText?.trim() || '',
                content: q('#js_content')?.innerText || q('.rich_media_content')?.innerText || ''
            };
        }""")

        context.close()
        browser.close()

    if not result.get("content"):
        result["error"] = "Failed to extract article content — possibly blocked or invalid URL"

    return result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: read_wechat.py <mp.weixin.qq.com URL>", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    if "mp.weixin.qq.com" not in url:
        print(f"Warning: URL does not look like a WeChat article: {url}", file=sys.stderr)

    result = read_article(url)
    print(json.dumps(result, ensure_ascii=False, indent=2))
