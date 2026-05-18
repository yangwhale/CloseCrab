# Dependency Management

CloseCrab pins all Python dependencies (top-level + transitive) for supply-chain safety.

## Files

| File | Maintained by | Purpose |
|------|---------------|---------|
| `base.in` | humans | Top-level base deps (what we import directly) |
| `voice.in` | humans | Voice-only top-level deps (LiveKit, ~200MB) |
| `base.lock` | `scripts/lock-deps.sh` | Full transitive closure, **EXACT pinned** |
| `voice.lock` | `scripts/lock-deps.sh` | Voice-only delta (excludes base) |

`deploy.sh` installs from `.lock` files. The `.in` files are human-readable manifests for "what we depend on at the top".

## Why pin transitives (not just top-level)

Shai-Hulud-class supply-chain attacks typically compromise a **transitive** dependency — some small package deep in the dep tree, not the top-level libs you import. Pinning only top-level packages gives false security: the next `pip install` still pulls whatever the transitive resolver decides.

Pinning the full closure freezes a known-good snapshot. Upgrades happen explicitly via the lock workflow below.

## Adding a new top-level dependency

```bash
# 1. Edit base.in (or voice.in) — add the package name on its own line
vim requirements/base.in

# 2. Install it locally first to make sure it works
pip install --break-system-packages <pkg>

# 3. Regenerate the lock from current install
./scripts/lock-deps.sh

# 4. Test deploy locally
./deploy.sh --bot

# 5. Review the diff (transitive deps may have been added)
git diff requirements/

# 6. Commit
git commit -am "deps: add <pkg> for <reason>"
```

## Upgrading an existing dependency

```bash
# 1. Bump locally
pip install --break-system-packages --upgrade <pkg>

# 2. Regenerate lock
./scripts/lock-deps.sh

# 3. Test
./deploy.sh --bot

# 4. Review diff (often pulls in new transitive versions)
git diff requirements/

# 5. Commit with reason
git commit -am "deps: bump <pkg> <old>→<new> (security/feature/...)"
```

## CI / drift check

```bash
# Verify installed packages match the lock — non-zero exit if drift detected
./scripts/lock-deps.sh --check
```

Run this periodically to catch silent drift (e.g. someone `pip install`d a dep without updating the lock).

## Caveats

- `.lock` is computed from `importlib.metadata` reflection of the **currently installed** packages, NOT from a clean resolver run. This means: if you install with `--no-deps` or have broken installs, the lock may be incomplete. Always run `lock-deps.sh` immediately after a clean `pip install`.
- We do NOT pin hashes (yet). Adding `--hash=sha256:...` would catch tampering of the artifact itself, but requires `pip-tools` or `uv pip compile`. Future enhancement when we move to a venv-based deployment.
- System packages (Ubuntu apt installs that happen to be Python) are deliberately excluded — `lock-deps.sh` walks only the dep closure of `*.in` packages, not the entire `pip freeze`.

## Migrating an old bot machine

Old machines (deployed before lock files existed) can be upgraded by:

```bash
git pull
./deploy.sh --bot
```

`deploy.sh` will detect `requirements/base.lock`, install pinned versions on top of existing ones. `pip` will silently upgrade/downgrade as needed.
