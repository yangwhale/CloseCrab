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
CHECK_ONLY=0
[[ "${1:-}" == "--check" ]] && CHECK_ONLY=1

PROFILE_DIR="$DSH_HOME/profiles/$PROFILE"
export DSH_HOME

say() { printf '  %s\n' "$*"; }
die() { printf 'dsh-setup: %s\n' "$*" >&2; exit 1; }

# ── prerequisites ──────────────────────────────────────────────────
command -v node >/dev/null 2>&1 || die "node not found; dsh needs Node 22.19+"
NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]')
NODE_MINOR=$(node -p 'process.versions.node.split(".")[1]')
if [[ "$NODE_MAJOR" -lt 22 || ( "$NODE_MAJOR" -eq 22 && "$NODE_MINOR" -lt 19 ) ]]; then
  die "node $(node --version) is too old; dsh needs ^22.19 || >=24"
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
        models:
          - id: claude-opus-5
            contextWindow: 1000000
            maxOutput: 32000
          - id: claude-sonnet-5
            contextWindow: 1000000
            maxOutput: 32000
          - id: gemini-3.7-flash
            contextWindow: 1000000
            maxOutput: 32000

- id: agent-default-model
  config:
    provider: litellm
    model: claude-opus-5

- insert:
    # Persistent session + streaming events over stdio. Without this the
    # profile boots and immediately has nothing to talk to.
    - id: sdk-jsonrpc-server
      name: '@deepseek-ai/dsh-sdk-jsonrpc-server'
EOF

# MCP is opt-in by design in dsh: every server is trusted code running outside
# the agent sandbox. Only wire jina in when a token is actually available.
if [[ -n "${JINA_AUTH:-}" ]]; then
  cat >> "$PATCH" <<'EOF'
    - id: mcp-jina
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: jina
        transport: streamable-http
        url: https://mcp.jina.ai/v1
        headers:
          Authorization: !!js process.env.JINA_AUTH
        toolCallTimeoutMs: 120000
EOF
  say "jina MCP wired in (JINA_AUTH present)"
else
  say "JINA_AUTH not set — skipping the jina MCP row"
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
