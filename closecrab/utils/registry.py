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

"""Bot self-registration to Firestore bot_registry.

On startup, each bot collects local machine info and updates its record
in the shared registry. This keeps the registry always up-to-date
with actual runtime state.
"""

import datetime
import logging
import os
import platform
import re
import shutil
import socket
import subprocess

log = logging.getLogger("closecrab.registry")


def _collect_machine_info(bot_name: str, cfg: dict) -> dict:
    """Collect local machine info for registry update."""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    # Hostname & IP
    hostname = socket.gethostname()
    try:
        ip = subprocess.check_output(
            ["hostname", "-I"], text=True, timeout=5
        ).strip().split()[0]
    except Exception:
        ip = ""

    # OS
    os_info = f"Linux {platform.release().split('-')[0]}"
    # Try to get distro
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    distro = line.split("=", 1)[1].strip().strip('"')
                    os_info = f"{distro}"
                    break
    except Exception:
        pass

    # CPU
    cpu = ""
    try:
        out = subprocess.check_output(
            ["lscpu"], text=True, timeout=5
        )
        for line in out.splitlines():
            if "Model name" in line:
                cpu = line.split(":", 1)[1].strip()
                break
        # Add core count
        cores_match = re.search(r"CPU\(s\):\s+(\d+)", out)
        if cores_match:
            cpu += f" {cores_match.group(1)}c"
    except Exception:
        pass

    # Memory (GB) - read from /proc/meminfo (locale-independent)
    memory_gb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    memory_gb = kb // (1024 * 1024)
                    break
    except Exception:
        pass

    # Accelerator detection
    accel_type = "none"
    accel_model = "none"
    accel_count = 0
    accel_mem_each_gb = 0
    accel_mem_total_gb = 0
    accel_interconnect = "none"
    accelerator = "none"

    # GPU detection (nvidia-smi)
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=10,
            )
            gpus = [line.strip() for line in out.strip().splitlines() if line.strip()]
            if gpus:
                accel_type = "GPU"
                accel_count = len(gpus)
                # Parse first GPU
                parts = gpus[0].split(",")
                accel_model = parts[0].strip()
                accel_mem_each_gb = int(float(parts[1].strip()) / 1024) if len(parts) > 1 else 0
                accel_mem_total_gb = accel_mem_each_gb * accel_count
                accelerator = f"{accel_model} x{accel_count}"
                # NVLink detection
                try:
                    nvlink_out = subprocess.check_output(
                        ["nvidia-smi", "nvlink", "-s"], text=True, timeout=5,
                    )
                    if "NVLink" in nvlink_out or accel_count > 1:
                        accel_interconnect = "NVLink"
                except Exception:
                    if accel_count > 1:
                        accel_interconnect = "NVLink"
        except Exception:
            pass

    # TPU detection (metadata server)
    if accel_type == "none":
        try:
            import requests
            r = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/attributes/accelerator-type",
                headers={"Metadata-Flavor": "Google"}, timeout=3,
            )
            if r.status_code == 200 and r.text:
                tpu_type = r.text.strip()  # e.g., "v6e-8"
                accel_type = "TPU"
                accel_model = tpu_type.split("-")[0] if "-" in tpu_type else tpu_type  # "v6e"
                # Parse chip count from type string
                count_match = re.search(r"-(\d+)", tpu_type)
                accel_count = int(count_match.group(1)) if count_match else 1
                # TPU HBM per chip (known specs)
                hbm_per_chip = {"v6e": 16, "v6": 32, "v5e": 16, "v5p": 96, "v4": 32, "v7": 192}
                accel_mem_each_gb = hbm_per_chip.get(accel_model, 16)
                accel_mem_total_gb = accel_mem_each_gb * accel_count
                accel_interconnect = "ICI"
                accelerator = f"TPU {tpu_type}"
        except Exception:
            pass

    # Host alias from config
    host = cfg.get("host", bot_name)

    return {
        "role": cfg.get("team", {}).get("role", "standalone") if cfg.get("team") else "standalone",
        "email": os.environ.get(cfg.get("email", {}).get("user_env", "FEISHU_SMTP_USER"), ""),
        "channel": cfg.get("channel", "discord"),
        "model": cfg.get("model", ""),
        "host": host,
        "hostname": hostname,
        "ip": ip,
        "os": os_info,
        "cpu": cpu,
        "accelerator": accelerator,
        "accel_type": accel_type,
        "accel_model": accel_model,
        "accel_count": accel_count,
        "accel_mem_each_gb": accel_mem_each_gb,
        "accel_mem_total_gb": accel_mem_total_gb,
        "accel_interconnect": accel_interconnect,
        "memory_gb": memory_gb,
        "status": "online",
        "last_seen": now,
    }


def register_bot(bot_name: str, cfg: dict):
    """Update bot's record in the registry with current machine info.

    Args:
        bot_name: Bot name (must match bot_name field in registry)
        cfg: Bot config dict (from _resolve_config)
    """
    inbox_cfg = cfg.get("inbox") or {}
    _register_firestore(bot_name, cfg, inbox_cfg)


def _register_firestore(bot_name: str, cfg: dict, inbox_cfg: dict):
    """Register bot info to Firestore registry collection."""
    try:
        from google.cloud import firestore
    except ImportError:
        log.warning("google-cloud-firestore not installed, skipping registry update")
        return

    from ..constants import FIRESTORE_PROJECT, FIRESTORE_DATABASE
    project = inbox_cfg.get("project", FIRESTORE_PROJECT)
    database = inbox_cfg.get("database", FIRESTORE_DATABASE)

    info = _collect_machine_info(bot_name, cfg)
    info["bot_name"] = bot_name

    try:
        db = firestore.Client(project=project, database=database)
        db.collection("registry").document(bot_name).set(info, merge=True)
        log.info(f"Registry updated (Firestore): {bot_name} @ {info['hostname']} ({info['accelerator']})")
    except Exception as e:
        log.warning(f"Firestore registry update error: {e}")