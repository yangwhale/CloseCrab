#!/usr/bin/env python3
"""gcall — 通过 ssh 直接驱动 gLinux 上的 Google 内部工具后端（替代 mcp-proxy）。

用法:
    gcall.py <server> <tool> '<json-args>'
    gcall.py <server> --list
    gcall.py --servers

例:
    gcall.py coding internal_search '{"query":"hybridsim","max_num_results":3}'
    gcall.py coding search_for_files_codesearch '{"query":"file:hybrid_sim"}'
    gcall.py workspace read_document '{"doc_id":"1abc..."}'
    gcall.py c2xprof c2xprof_upload '{"gcs_path":"gs://...","project":"chris-pgp-host"}'

为什么不走 MCP: mcp-proxy 驱动 coding_server.par 时 Gaia mint 必失败
(见 memory feedback_mcp-proxy-gaia-mint-fails)，而 ssh 直连 stdio 100% 可用；
顺带省掉 MCP tool schema 的冷启动 token。
"""
import json
import os
import subprocess
import sys

GHOME = "/usr/local/google/home/chrisya"

# 已经在 gLinux 上（hulk / tommy）就直接跑本地进程，不要 ssh 自己一圈。
# 判据用 GHOME 是否存在，比 hostname 前缀稳 —— 换机器名不用改代码。
ON_GLINUX = os.path.isdir(GHOME)
SSH_HOST = "glinux"

SERVERS = {
    "coding": ["python3", f"{GHOME}/.local/bin/coding-server-wrapper.py"],
    "workspace": ["/google/bin/releases/codemind-mcp-servers/workspace_server.par"],
    "c2xprof": ["python3", f"{GHOME}/.claude/scripts/c2xprof-mcp-server.py"],
    "bugged": ["python3", f"{GHOME}/.claude/scripts/bugged-mcp-server.py"],
}

TIMEOUT = 300


def drive(server, method, params):
    cmd = SERVERS[server] if ON_GLINUX else ["ssh", "-T", SSH_HOST] + SERVERS[server]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, bufsize=1)

    def send(o):
        p.stdin.write(json.dumps(o) + "\n")
        p.stdin.flush()

    def read():
        while True:
            line = p.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "gcall", "version": "1"}}})
        if read() is None:
            return None, "后端没响应 initialize（ssh 通吗？gcert 过期了吗？）"
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        return read(), None
    finally:
        try:
            p.kill()
        except Exception:
            pass


def main():
    a = sys.argv[1:]
    if not a or a[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if a[0] == "--servers":
        print(" ".join(SERVERS))
        return 0

    server = a[0]
    if server not in SERVERS:
        print(f"未知 server: {server}；可选: {' '.join(SERVERS)}", file=sys.stderr)
        return 2

    if len(a) > 1 and a[1] == "--list":
        r, err = drive(server, "tools/list", {})
        if err:
            print(err, file=sys.stderr)
            return 1
        for t in (r or {}).get("result", {}).get("tools", []):
            sch = t.get("inputSchema", {})
            req = ",".join(sch.get("required", []))
            allp = ",".join(sch.get("properties", {}).keys())
            print(f"{t['name']}\n    必填: {req or '(无)'}\n    全部: {allp}")
        return 0

    if len(a) < 2:
        print("缺 tool 名。用 --list 看可用工具。", file=sys.stderr)
        return 2

    tool = a[1]
    try:
        args = json.loads(a[2]) if len(a) > 2 else {}
    except json.JSONDecodeError as e:
        print(f"参数不是合法 JSON: {e}", file=sys.stderr)
        return 2

    r, err = drive(server, "tools/call", {"name": tool, "arguments": args})
    if err:
        print(err, file=sys.stderr)
        return 1
    if r is None:
        print("后端无响应", file=sys.stderr)
        return 1
    if "error" in r:
        print(json.dumps(r["error"], ensure_ascii=False, indent=1), file=sys.stderr)
        return 1
    for item in r.get("result", {}).get("content", []):
        if item.get("type") == "text":
            print(item["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
