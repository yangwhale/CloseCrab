"""在本机用真浏览器跑一遍前端，复现/验证「20 秒判死」那个握手死线。

用法：
    LK_SFU_URL=ws://<SFU 内网地址>:7880 python3 lk_probe.py <房间名> [秒数]

**判定标准是「跑完全程没出现 Session ended」**，不是「页面显示 Agent is
listening」—— 那两句话会同时出现，正是这个 bug 最迷惑人的地方（页面上的
"listening" 来自 useVoiceAssistant，它直读参与者属性；判死的是 useAgent，
它只靠属性变更事件学，学不到就一直以为对方在 connecting）。

**最该跑的用例是「断开后立刻重连」**：那一轮 agent 身上已经粘着
lk.agent.state=listening，不会再有任何属性变更事件，能过才说明前端补丁生效。

两处改道：token 响应里的 serverUrl 换成 SFU 内网 ws 地址（公网那个在 IAP
后面，headless 没有 cookie 进不去），以及假麦克风。
"""
import asyncio, json, os, sys, time
from playwright.async_api import async_playwright

# 不给默认值：这个地址随部署而变，猜一个只会得到一次沉默的连不上。
SFU_URL = os.environ.get("LK_SFU_URL")
if not SFU_URL:
    sys.exit("要设 LK_SFU_URL，例如 ws://10.0.0.1:7880（SFU 的**内网** ws 地址）")

ROOM = sys.argv[1] if len(sys.argv) > 1 else "hulk"
WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 32

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ])
        ctx = await b.new_context(permissions=["microphone"])
        t0 = time.time()
        async def route_token(route):
            r = await route.fetch()
            d = await r.json()
            d["serverUrl"] = SFU_URL
            print(f"[{time.time()-t0:6.2f}] token → room={d.get('roomName')} identity={d.get('participantName')}")
            await route.fulfill(json=d)
        await ctx.route("**/api/token**", route_token)
        pg = await ctx.new_page()
        pg.on("console", lambda m: print(f"[{time.time()-t0:6.2f}] console.{m.type}: {m.text[:300]}"))
        pg.on("pageerror", lambda e: print(f"[{time.time()-t0:6.2f}] pageerror: {e}"))
        await pg.goto(f"http://127.0.0.1:3000/?room={ROOM}", wait_until="domcontentloaded")
        btn = pg.get_by_role("button", name="START CALL")
        await btn.click(timeout=15000)
        print(f"[{time.time()-t0:6.2f}] 点了 START CALL")
        seen = set()
        end = time.time() + WAIT
        while time.time() < end:
            txt = (await pg.inner_text("body")).strip().replace("\n", " / ")
            if txt not in seen:
                seen.add(txt)
                print(f"[{time.time()-t0:6.2f}] 页面: {txt[:200]}")
            await asyncio.sleep(0.4)
        await b.close()

asyncio.run(main())
