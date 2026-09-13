"""同 lk_probe.py，外加一层 setTimeout / clearTimeout 钩子 ——
用来回答「那个 20 秒定时器是谁、在什么时候上的弦」。

用法：LK_SFU_URL=ws://<SFU 内网地址>:7880 python3 lk_probe2.py <房间名>
"""
import os, sys
SFU_URL = os.environ.get("LK_SFU_URL")
if not SFU_URL:
    sys.exit("要设 LK_SFU_URL，例如 ws://10.0.0.1:7880（SFU 的**内网** ws 地址）")
import asyncio, sys, time
from playwright.async_api import async_playwright
ROOM = sys.argv[1] if len(sys.argv)>1 else "hulk"

INIT = r"""
(() => {
  const t0 = Date.now();
  const st = window.setTimeout;
  window.setTimeout = function (fn, d, ...a) {
    if (d >= 19000 && d <= 21000) {
      const stack = new Error().stack.split('\n').slice(1,5).join(' ⏎ ');
      const id = st.call(window, function(){ console.log('[PROBE] TIMER FIRED +'+((Date.now()-t0)/1000).toFixed(2)); return fn.apply(this, arguments); }, d, ...a);
      console.log('[PROBE] ARM id='+id+' +'+((Date.now()-t0)/1000).toFixed(2)+' :: '+stack);
      return id;
    }
    return st.call(window, fn, d, ...a);
  };
  const ct = window.clearTimeout;
  window.clearTimeout = function (id) {
    console.log('[PROBE] CLEAR id='+id+' +'+((Date.now()-t0)/1000).toFixed(2));
    return ct.call(window, id);
  };
})();
"""

async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream"])
        ctx = await b.new_context(permissions=["microphone"])
        await ctx.add_init_script(INIT)
        t0=time.time()
        async def rt(route):
            r = await route.fetch(); d = await r.json()
            d["serverUrl"]=SFU_URL
            await route.fulfill(json=d)
        await ctx.route("**/api/token**", rt)
        pg = await ctx.new_page()
        pg.on("console", lambda m: print(f"[{time.time()-t0:6.2f}] {m.text[:400]}") if "[PROBE]" in m.text else None)
        await pg.goto(f"http://127.0.0.1:3000/?room={ROOM}", wait_until="domcontentloaded")
        await pg.get_by_role("button", name="START CALL").click(timeout=15000)
        print(f"[{time.time()-t0:6.2f}] clicked")
        await asyncio.sleep(25)
        print("页面:", (await pg.inner_text("body")).replace("\n"," / ")[:240])
        await b.close()
asyncio.run(main())
