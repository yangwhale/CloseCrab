#!/usr/bin/env python3
"""Export current canvas state to a self-contained HTML on CC Pages.

Usage:
    python3 scripts/export-canvas.py [--topic TOPIC] [--animate]

    --topic: slug for the filename (default: "demo")
    --animate: bake in step-by-step animation with CSS delays
"""
import argparse
import json
import pathlib
import time
import urllib.request

CANVAS_TEMPLATE = pathlib.Path(__file__).parent.parent.parent.parent / "tools" / "board-canvas.html"
CC_PAGES = pathlib.Path.home() / "gcs-mount" / "cc-pages" / "pages"
PUBLISH_SCRIPT = pathlib.Path.home() / "CloseCrab" / "scripts" / "publish-cc-page.sh"


def fetch_state(port=8766):
    resp = urllib.request.urlopen(f"http://localhost:{port}/canvas/api/state")
    return json.loads(resp.read())


def build_static_page(state, animate=False):
    template = CANVAS_TEMPLATE.read_text()

    inject = f"""
<script>
const STATE = {json.dumps(state, ensure_ascii=False)};
handleCmd(STATE);
dotEl.className = 'dot on'; connText.textContent = '已加载';
</script>
"""
    html = template.replace('connect(); startPolling();', '// static export')
    html = html.replace('</body>', inject + '</body>')
    return html


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--topic", default="demo")
    p.add_argument("--animate", action="store_true")
    p.add_argument("--port", type=int, default=8766)
    args = p.parse_args()

    state = fetch_state(args.port)
    n = len(state.get("elements", []))
    print(f"Fetched canvas: {n} elements, title={state.get('title','')}")

    html = build_static_page(state, args.animate)

    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"canvas-{args.topic}-{ts}.html"
    out = CC_PAGES / filename
    out.write_text(html)
    print(f"Written to {out}")

    import subprocess
    if PUBLISH_SCRIPT.exists():
        r = subprocess.run([str(PUBLISH_SCRIPT), "--force", str(out)],
                          capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if "cc.higcp.com" in line:
                print(line.strip())

    print(f"\nhttps://cc.higcp.com/pages/{filename}")


if __name__ == "__main__":
    main()
