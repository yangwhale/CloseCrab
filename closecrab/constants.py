# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""CloseCrab global constants.

Bootstrap config (Firestore connection) is read from env vars / .env file.
Business constants are read from Firestore config/global document, cached locally.

On first run, if .env is missing, interactive setup guides the user.
"""

import os
from pathlib import Path

# ── Bootstrap: Firestore connection ──

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _load_dotenv():
    """Read .env file (KEY=VALUE format only, no third-party deps)."""
    if not _ENV_FILE.exists():
        return
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def _setup_bootstrap():
    """Ensure FIRESTORE_PROJECT and FIRESTORE_DATABASE are available.

    Priority: env var > .env file > interactive prompt.
    """
    _load_dotenv()

    project = os.environ.get("FIRESTORE_PROJECT")
    database = os.environ.get("FIRESTORE_DATABASE")

    if project and database:
        return project, database

    import sys
    if not sys.stdin.isatty():
        if not project or not database:
            raise RuntimeError(
                "FIRESTORE_PROJECT and FIRESTORE_DATABASE must be set. "
                "Add them to .env or export as environment variables."
            )

    print("=" * 50)
    print("CloseCrab First-time Setup")
    print("=" * 50)
    print()
    if not project:
        project = input("Firestore Project ID: ").strip()
        if not project:
            raise RuntimeError("FIRESTORE_PROJECT is required.")
    if not database:
        database = input("Firestore Database ID [closecrab]: ").strip() or "closecrab"

    # Write to .env
    lines = []
    if _ENV_FILE.exists():
        lines = _ENV_FILE.read_text().splitlines()

    env_vars = {"FIRESTORE_PROJECT": project, "FIRESTORE_DATABASE": database}
    existing_keys = set()
    for i, line in enumerate(lines):
        for key in env_vars:
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={env_vars[key]}"
                existing_keys.add(key)
    for key, value in env_vars.items():
        if key not in existing_keys:
            lines.append(f"{key}={value}")

    _ENV_FILE.write_text("\n".join(lines) + "\n")
    print(f"\nSaved to {_ENV_FILE}")

    os.environ["FIRESTORE_PROJECT"] = project
    os.environ["FIRESTORE_DATABASE"] = database
    return project, database


FIRESTORE_PROJECT, FIRESTORE_DATABASE = _setup_bootstrap()


# ── Business constants: read from Firestore config/global ──

_global_config: dict | None = None


def _load_global_config() -> dict:
    """Read global config from Firestore, cache in module-level variable."""
    global _global_config
    if _global_config is not None:
        return _global_config

    try:
        from google.cloud import firestore
        db = firestore.Client(project=FIRESTORE_PROJECT, database=FIRESTORE_DATABASE)
        doc = db.collection("config").document("global").get()
        _global_config = doc.to_dict() if doc.exists else {}
    except Exception:
        _global_config = {}

    return _global_config


def get(key: str, default: str = "") -> str:
    """Read a global config value."""
    cfg = _load_global_config()
    return cfg.get(key, default)


class _Const:
    """Lazy-loaded constant proxy. Values read from Firestore on first access,
    with env var overrides."""

    @property
    def CC_PAGES_URL(self) -> str:
        return os.environ.get("CC_PAGES_URL_PREFIX") or get("cc_pages_url", "")

    @property
    def GCP_PROJECT(self) -> str:
        return os.environ.get("GOOGLE_CLOUD_PROJECT") or get("gcp_project", "")

    @property
    def GCS_BUCKET(self) -> str:
        return os.environ.get("GCS_BUCKET") or get("gcs_bucket", "")


G = _Const()