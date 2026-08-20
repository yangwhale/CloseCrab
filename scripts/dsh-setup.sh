#!/usr/bin/env bash
# Build the DeepSeek Harness profile the `dsh` worker drives.
#
# Why a custom profile at all: dsh ships two profiles, `web` (a browser UI) and
# `headless` (one task, then exit). Neither keeps a session alive, which is what
# a chat bot needs. This builds a third that mounts
# @deepseek-ai/dsh-sdk-jsonrpc-server, the same plugin the Python SDK talks to,
# on top of dsh-base's full tool surface — so the worker gets a persistent
# session, streaming events, skills, and MCP from one process.
#
# The Python SDK's own bundled runtime is NOT an alternative here: it is a
# sealed binary without dsh-mcp-client compiled in, and mounting that plugin
# there hangs the host with no error message.
#
# Idempotent: safe to re-run. Pass --check to verify without changing anything.

set -euo pipefail

DSH_HOME="${DSH_HOME:-$HOME/.closecrab/dsh-home}"
PROFILE="${DSH_PROFILE:-closecrab}"
# Public HTTPS endpoint, not the old 127.0.0.1 SSH tunnel: ~0.5s slower per
# round trip, but no tunnel to keep alive and it works from any host.
LITELLM_URL="${LITELLM_BASE_URL:-https://litellm.higcp.com/v1}"
DSH_PKG="@deepseek-ai/dsh@latest"
# The MCP row needs a full Authorization header; the fleet stores the bare key.
# Derive rather than ask for a second copy that can drift out of sync.
if [[ -z "${JINA_AUTH:-}" && -n "${JINA_API_KEY:-}" ]]; then
  JINA_AUTH="Bearer $JINA_API_KEY"
  export JINA_AUTH
fi
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

PROFILE_DIR="$DSH_HOME/profiles/$PROFILE"
export DSH_HOME

say() { printf '  %s\n' "$*"; }
die() { printf 'dsh-setup: %s\n' "$*" >&2; exit 1; }

# ── prerequisites ──────────────────────────────────────────────────
# Match run.sh: it prepends the HIGHEST nvm version, not nvm's `default` alias.
# On a box where default is v20 but v22/v24 are installed, checking the alias
# concludes "this host cannot run dsh" while the bot runtime happily has v24.
if [[ -d "$HOME/.nvm/versions/node" ]]; then
  NVM_DIR_LATEST="$(ls -d "$HOME/.nvm/versions/node"/v* 2>/dev/null | sort -V | tail -1)"
  [[ -n "$NVM_DIR_LATEST" ]] && export PATH="$NVM_DIR_LATEST/bin:$PATH"
fi
command -v node >/dev/null 2>&1 || die "node not found; dsh needs Node 22.19+"
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
NODE_MINOR=$(node -p 'process.versions.node.split(".")[1]')
if [[ "$NODE_MAJOR" -lt 22 || ( "$NODE_MAJOR" -eq 22 && "$NODE_MINOR" -lt 19 ) ]]; then
  die "node $(node --version) is too old; dsh needs ^22.19 || >=24"
fi

# dsh manages profile plugins by shelling out to pnpm, and says only
# "pnpm not found on PATH" when it is missing -- after it has already created
# the profile directory, so a re-run looks like it is resuming rather than
# starting over. Install it rather than failing the deploy on a one-liner.
if ! command -v pnpm >/dev/null 2>&1; then
  echo "  pnpm not found, installing into ~/.npm-global"
  npm install -g --prefix "$HOME/.npm-global" pnpm >/dev/null 2>&1 || true
  export PATH="$HOME/.npm-global/bin:$PATH"
  command -v pnpm >/dev/null 2>&1 || die "pnpm install failed; dsh cannot manage profile plugins"
fi

dsh_run() {
  if command -v dsh >/dev/null 2>&1; then dsh "$@"; else npx -y "$DSH_PKG" "$@"; fi
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "dsh-setup --check"
  say "node:        $(node --version)"
  say "dsh:         $(dsh_run --version 2>/dev/null || echo 'NOT INSTALLED')"
  say "DSH_HOME:    $DSH_HOME"
  say "profile dir: $PROFILE_DIR $([[ -d "$PROFILE_DIR" ]] && echo '(present)' || echo '(MISSING)')"
  say "patch file:  $([[ -f "$PROFILE_DIR/cordis.patch.yml" ]] && echo present || echo MISSING)"
  say "jsonrpc srv: $([[ -d "$PROFILE_DIR/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-server" ]] \
        && echo present || echo MISSING)"
  exit 0
fi

echo "dsh-setup: profile '$PROFILE' under $DSH_HOME"
mkdir -p "$DSH_HOME/profiles"

# ── 1. the profile skeleton ────────────────────────────────────────
# `dsh plugin --profile <new> add @deepseek-ai/dsh-base` does NOT work: dsh-base
# pulls @deepseek-ai/dsh-settings-local, which is not published, so pnpm 404s.
# Bundles resolve from the dsh installation anyway, so the profile only has to
# *name* dsh-base — it must not depend on it.
if [[ ! -f "$PROFILE_DIR/package.json" ]]; then
  # Let dsh create its own headless template first (it self-initializes), then
  # fork it. Writing package.json by hand risks drifting from the template.
  say "bootstrapping headless template so the profile skeleton matches dsh's own"
  dsh_run --profile headless --dump-default-config >/dev/null 2>&1 || true
  if [[ -d "$DSH_HOME/profiles/headless" ]]; then
    cp -a "$DSH_HOME/profiles/headless" "$PROFILE_DIR"
    rm -rf "$PROFILE_DIR/node_modules"
  else
    mkdir -p "$PROFILE_DIR"
  fi
  python3 - "$PROFILE_DIR/package.json" "$PROFILE" <<'PY'
import json, sys, pathlib
path, name = pathlib.Path(sys.argv[1]), sys.argv[2]
d = json.loads(path.read_text()) if path.exists() else {"private": True, "dependencies": {}}
d["name"] = f"dsh-profile-{name}"
# dsh-headless would mount the one-shot runner and exit; we want base only.
d.setdefault("dsh", {}).setdefault("profile", {})["bundles"] = ["@deepseek-ai/dsh-base"]
path.write_text(json.dumps(d, indent=2) + "\n")
PY
  say "created $PROFILE_DIR"
else
  say "profile dir already present"
fi

# ── 2. the JSON-RPC server plugin ──────────────────────────────────
# It declares no dsh.bundle, so it installs as a plain dependency and gets
# mounted by the patch below. dsh-sdk-protocol is a peer it does not pull in.
for pkg in @deepseek-ai/dsh-sdk-jsonrpc-server @deepseek-ai/dsh-sdk-protocol; do
  short="${pkg##*/}"
  if [[ -d "$PROFILE_DIR/node_modules/@deepseek-ai/$short" ]]; then
    say "$short already installed"
  else
    say "installing $short"
    dsh_run plugin --profile "$PROFILE" add "$pkg" >/dev/null 2>&1 \
      || die "failed to install $pkg into profile $PROFILE"
  fi
done

# Patch dsh-sdk-jsonrpc-server to support native multimodal file blocks
python3 - "$PROFILE_DIR/node_modules/@deepseek-ai/dsh-sdk-jsonrpc-server/lib/index.js" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
if not p.exists():
    sys.exit(0)
code = p.read_text(encoding="utf-8")
if 'node:fs/promises' not in code:
    code = 'import * as fs from "node:fs/promises";\nimport { extname, basename } from "node:path";\n' + code

old_prompt = """\tasync prompt(params) {
\t\tconst rec = await this.getOrCreateSession(params.sessionId);
\t\tif (this.ctx.agents.get(rec.handle.agent.id) !== rec.handle.agent) throw new Error(`session agent was disposed outside the server: ${params.sessionId}`);
\t\tconst message = createUserMessage({
\t\t\tcontent: params.contentBlocks,
\t\t\tsource: { kind: "user" }
\t\t});
\t\trec.handle.agent.followup(message);
\t\treturn { messageId: message.id };
\t}"""

new_prompt = """\tasync prompt(params) {
\t\tconst rec = await this.getOrCreateSession(params.sessionId);
\t\tif (this.ctx.agents.get(rec.handle.agent.id) !== rec.handle.agent) throw new Error(`session agent was disposed outside the server: ${params.sessionId}`);
\t\tconst attachments = this.ctx.get("attachments");
\t\tconst contentBlocks = [];
\t\tconst extMimes = {
\t\t\t".pdf": "application/pdf",
\t\t\t".wav": "audio/wav",
\t\t\t".ogg": "audio/ogg",
\t\t\t".mp3": "audio/mpeg",
\t\t\t".m4a": "audio/mp4",
\t\t\t".flac": "audio/flac",
\t\t\t".mp4": "video/mp4",
\t\t\t".webm": "video/webm",
\t\t\t".mov": "video/quicktime",
\t\t\t".png": "image/png",
\t\t\t".jpg": "image/jpeg",
\t\t\t".jpeg": "image/jpeg",
\t\t\t".webp": "image/webp",
\t\t\t".gif": "image/gif"
\t\t};
\t\tfor (const block of (params.contentBlocks || [])) {
\t\t\tif (block && block.type === "file" && block.path && attachments) {
\t\t\t\ttry {
\t\t\t\t\tconst ext = extname(block.path).toLowerCase();
\t\t\t\t\tconst mediaType = block.mediaType || extMimes[ext] || "application/octet-stream";
\t\t\t\t\tconst data = await fs.readFile(block.path);
\t\t\t\t\tconst ref = await attachments.saveImage({
\t\t\t\t\t\tdata,
\t\t\t\t\t\tmediaType,
\t\t\t\t\t\tname: block.name || basename(block.path)
\t\t\t\t\t});
\t\t\t\t\tcontentBlocks.push({
\t\t\t\t\t\ttype: "image",
\t\t\t\t\t\tattachment: ref
\t\t\t\t\t});
\t\t\t\t} catch (err) {
\t\t\t\t\tcontentBlocks.push({
\t\t\t\t\t\ttype: "text",
\t\t\t\t\t\ttext: `[Failed to load attachment ${block.path}: ${err.message}]`
\t\t\t\t\t});
\t\t\t\t}
\t\t\t} else {
\t\t\t\tcontentBlocks.push(block);
\t\t\t}
\t\t}
\t\tconst message = createUserMessage({
\t\t\tcontent: contentBlocks,
\t\t\tsource: { kind: "user" }
\t\t});
\t\trec.handle.agent.followup(message);
\t\treturn { messageId: message.id };
\t}"""

if old_prompt in code:
    code = code.replace(old_prompt, new_prompt)
    p.write_text(code, encoding="utf-8")
    print("  patched dsh-sdk-jsonrpc-server for native multimodal attachment support")
PY

# ── 3. the patch layer ─────────────────────────────────────────────
# A patch REPLACES a row's whole `config` (no deep merge), and a bare `- id:`
# for a row that does not exist yet only logs "entry not found" and carries on
# — adding rows needs the `insert:` form. Both are easy to get wrong silently.
PATCH="$PROFILE_DIR/cordis.patch.yml"
if [[ -f "$PATCH" ]] && grep -q 'CloseCrab managed' "$PATCH"; then
  say "patch file already managed by this script — rewriting to stay current"
fi
cat > "$PATCH" <<EOF
# CloseCrab managed — regenerated by scripts/dsh-setup.sh, edits will be lost.
#
# A patch entry replaces the targeted row's ENTIRE config; it does not merge.
# A bare '- id:' targeting a row that does not exist is a no-op with a warning,
# so new rows go through 'insert:'.

# Models come from the LiteLLM gateway rather than DeepSeek's own API, so one
# key and one base URL cover Claude and Gemini alike.
- id: llm-pi-ai
  config:
    providers:
      litellm:
        api: openai-completions
        baseURL: $LITELLM_URL
        apiKeyEnv: LITELLM_KEY
        defaultInput: [text, image]
        models:
          - id: claude-opus-5
            contextWindow: 1000000
            maxOutput: 32000
            input: [text, image]
          - id: claude-sonnet-5
            contextWindow: 1000000
            maxOutput: 32000
            input: [text, image]
          - id: claude-haiku-4-5-20251001
            contextWindow: 200000
            maxOutput: 16000
            input: [text, image]
          - id: gemini-3.7-flash
            contextWindow: 1000000
            maxOutput: 32000
            input: [text, image]

- id: agent-default-model
  config:
    provider: litellm
    model: claude-opus-5

# ── Model tiering ──────────────────────────────────────────────────
# dsh has no named main/secondary/fast tier. Every consumer that opens an LLM
# request routes on its own, so a tier is something you compose here.
#
# Two ways to get this wrong, both fatal at boot rather than degraded:
#   * a patch REPLACES the row's whole config, so every original key has to be
#     copied back. Dropping session-title-llm's required timeoutMs takes the
#     entire plugin tree down -- the agent loses bash, not just titles.
#   * the key names are per-plugin. compaction-basic does NOT take
#     provider/model; it takes summarizationProvider/summarizationModel.
# And a model only works here if it is also declared in the provider catalog
# above: an undeclared one fails at call time as "subagent run failed".
- id: session-title-llm
  config:
    targetWords: 5
    targetCjkCharacters: 10
    maxInputBytes: 4096
    maxOutputTokens: 64
    timeoutMs: 60000
    provider: litellm
    model: claude-haiku-4-5-20251001

- id: compaction-basic
  config:
    summarizationProvider: litellm
    summarizationModel: claude-sonnet-5

- id: tool-subagent
  config:
    provider: spawn
    toolName: subagent
    backgroundMode: continuable
    agentOptions:
      provider: litellm
      model: claude-sonnet-5

- id: tool-subagent-fork
  config:
    provider: fork
    toolName: subagent_fork
    backgroundMode: one-shot
    agentOptions:
      provider: litellm
      model: claude-haiku-4-5-20251001

- insert:
    # Persistent session + streaming events over stdio. Without this the
    # profile boots and immediately has nothing to talk to.
    - id: sdk-jsonrpc-server
      name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
EOF

# MCP servers: sync from ~/.claude.json or wire standard servers (jina, wiki, serena)
python3 - "$PATCH" <<'PY'
import json, yaml, sys, os

patch_path = sys.argv[1]
claude_json_path = os.path.expanduser("~/.claude.json")
mcp_servers = {}
if os.path.exists(claude_json_path):
    try:
        with open(claude_json_path) as f:
            mcp_servers = json.load(f).get("mcpServers", {})
    except Exception as e:
        print(f"  warning: failed to read {claude_json_path}: {e}")

# Read existing patch
with open(patch_path) as f:
    patch = yaml.safe_load(f) or []

# Find the insert block
insert_block = None
for item in patch:
    if "insert" in item and isinstance(item["insert"], list):
        insert_block = item["insert"]
        break

if insert_block is None:
    insert_block = []
    patch.append({"insert": insert_block})

# Build MCP client entries
for name, srv in mcp_servers.items():
    srv_type = srv.get("type")
    # Clean server name for DSH namespace (e.g. jina-ai -> jina)
    server_name = "jina" if name == "jina-ai" else name
    entry_id = f"mcp-{server_name}"
    
    # Skip if already in insert block
    if any(e.get("id") == entry_id for e in insert_block if isinstance(e, dict)):
        continue
        
    if srv_type == "http":
        url = srv.get("url")
        headers = srv.get("headers", {})
        if not url:
            continue
        entry = {
            "id": entry_id,
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": server_name,
                "transport": "streamable-http",
                "url": url,
                "headers": headers,
                "toolCallTimeoutMs": 120000,
            }
        }
        insert_block.append(entry)
        print(f"  added HTTP MCP server: {server_name}")
    elif srv_type == "stdio":
        cmd = srv.get("command")
        args = srv.get("args", [])
        if not cmd:
            continue
        entry = {
            "id": entry_id,
            "name": "@deepseek-ai/dsh-mcp-client",
            "config": {
                "serverName": server_name,
                "transport": "stdio",
                "command": cmd,
                "args": args,
                "toolCallTimeoutMs": 60000,
            }
        }
        if "env" in srv:
            entry["config"]["env"] = srv["env"]
        if "cwd" in srv:
            entry["config"]["cwd"] = srv["cwd"]
        insert_block.append(entry)
        print(f"  added stdio MCP server: {server_name}")

with open(patch_path, "w") as f:
    yaml.dump(patch, f, sort_keys=False, default_flow_style=False)
PY

# ── private overlay ────────────────────────────────────────────────
# Anything that names an employer-internal tool — a server name, an absolute
# path to one, a port it listens on — belongs in the private skills tree, not
# here: this repository is public. The hook is deliberately dumb (source a
# file if it exists) so the public side carries no hint of what it appends.
#
# The overlay is sourced, so it can append to "$PATCH" directly. It runs after
# the base patch is written and before the smoke test, and its absence is
# normal, not an error.
PRIVATE_SKILLS_DIR="${PRIVATE_SKILLS_DIR:-$HOME/private-skills}"
PRIVATE_OVERLAY="$PRIVATE_SKILLS_DIR/dsh/profile-overlay.sh"
if [[ -f "$PRIVATE_OVERLAY" ]]; then
  say "applying private profile overlay"
  # shellcheck disable=SC1090
  source "$PRIVATE_OVERLAY"
else
  say "no private profile overlay (looked in $PRIVATE_OVERLAY)"
fi

say "wrote $PATCH"

# ── 4. smoke test ──────────────────────────────────────────────────
# "the config parses" is not the same as "the runtime answers", so actually
# speak the protocol.
if [[ -z "${LITELLM_KEY:-}" ]]; then
  say "LITELLM_KEY not set — skipping the live smoke test"
  say "done (profile built, unverified)"
  exit 0
fi

say "smoke testing the JSON-RPC handshake"
SMOKE=$(cd "$HOME" && printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"cwd":"'"$HOME"'","provider":"litellm","model":"claude-opus-5","maxTokens":1024}}' \
  | timeout 120 bash -c "$(command -v dsh >/dev/null 2>&1 && echo dsh || echo "npx -y $DSH_PKG") --profile $PROFILE" 2>&1 \
  | grep -m1 '"serverInfo"' || true)

if [[ -n "$SMOKE" ]]; then
  say "handshake OK: $SMOKE"
else
  die "handshake failed — run: DSH_HOME=$DSH_HOME dsh --profile $PROFILE  and read the error"
fi

echo "dsh-setup: done"
