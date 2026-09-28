#!/usr/bin/env python3
"""在页面的某个子 frame（按 URL 子串匹配）里执行 JS：cdp_frame.py <页面URL子串> <frameURL子串> <js表达式>"""
import asyncio, json, sys, urllib.request, websockets
pg_sub, fr_sub, js = sys.argv[1:4]
t = [x for x in json.load(urllib.request.urlopen('http://127.0.0.1:9222/json')) if x['type'] == 'page' and pg_sub in x['url']][0]
async def main():
    async with websockets.connect(t['webSocketDebuggerUrl'], max_size=2**26) as ws:
        n = [0]
        async def call(m, p=None):
            n[0] += 1; i = n[0]
            await ws.send(json.dumps({'id': i, 'method': m, 'params': p or {}}))
            while True:
                r = json.loads(await ws.recv())
                if r.get('id') == i: return r
        tree = (await call('Page.getFrameTree'))['result']['frameTree']
        frames = []
        def walk(f):
            frames.append(f['frame']); [walk(c) for c in f.get('childFrames', [])]
        walk(tree)
        fr = [f for f in frames if fr_sub in f.get('url', '')]
        if not fr: print('frames:', [f.get('url','')[:100] for f in frames]); return
        ctx = (await call('Page.createIsolatedWorld', {'frameId': fr[0]['id'], 'worldName': 'jarvis', 'grantUniveralAccess': True}))['result']['executionContextId']
        r = await call('Runtime.evaluate', {'expression': js, 'contextId': ctx, 'awaitPromise': True, 'returnByValue': True})
        print(json.dumps(r.get('result', {}).get('result', {}).get('value', r), ensure_ascii=False)[:4000])
asyncio.run(main())
