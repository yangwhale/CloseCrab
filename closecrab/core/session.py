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

"""Session persistence and management.

Extracted from bot.py session handling logic (L692-753).
"""

import json
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("closecrab.core.session")


class SessionManager:
    """管理用户 session 的持久化、归档和历史查询。

    Args:
        sessions_file: session 数据文件路径
        project_dir: Claude session .jsonl 文件所在目录（用于读取 summary）
    """

    def __init__(
        self,
        sessions_file: str = None,
        project_dir: str = None,
        state_dir: str = None,
    ):
        # state_dir 优先: 指定后 sessions_file 自动放在 state_dir 下
        if state_dir:
            state_path = Path(state_dir)
            self._sessions_file = Path(sessions_file) if sessions_file else state_path / "sessions.json"
        else:
            self._sessions_file = Path(
                sessions_file or str(Path.home() / ".claude/closecrab/sessions.json")
            )
        self._project_dir = Path(
            project_dir or self._detect_project_dir()
        )
        self._sessions_file.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _detect_project_dir() -> str:
        """自动检测 CC project 目录（gLinux vs GCE VM home 路径不同）。"""
        home = Path.home()
        # home 路径转 CC 项目目录名: /home/user -> -home-user
        # /usr/local/google/home/user -> -usr-local-google-home-user
        project_name = str(home).replace("/", "-")
        candidate = home / ".claude" / "projects" / project_name
        if candidate.exists():
            return str(candidate)
        # fallback: 扫描 projects 目录找最新的
        projects_dir = home / ".claude" / "projects"
        if projects_dir.exists():
            candidates = sorted(projects_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
            for c in candidates:
                if c.is_dir() and any(c.glob("*.jsonl")):
                    return str(c)
        return str(home / ".claude" / "projects" / project_name)

    def load(self) -> dict:
        """加载 session 数据。返回 {user_key: {active: str, history: [str]}}"""
        try:
            data = json.loads(self._sessions_file.read_text())
            # 兼容旧格式 (flat dict of user_key -> session_id)
            if data and not any(isinstance(v, dict) for v in data.values()):
                return {k: {"active": v, "history": []} for k, v in data.items()}
            return data
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self, active_sessions: dict[str, Optional[str]]):
        """保存活跃 session 信息。

        Args:
            active_sessions: {user_key: session_id} 映射
        """
        data = self.load()
        for user_key, session_id in active_sessions.items():
            if session_id:
                if user_key not in data:
                    data[user_key] = {"active": None, "history": []}
                data[user_key]["active"] = session_id
        self._sessions_file.write_text(json.dumps(data, indent=2))

    def archive(self, user_key: str, session_id: str):
        """将 session 存入历史。"""
        data = self.load()
        if user_key not in data:
            data[user_key] = {"active": None, "history": []}
        if session_id and session_id not in data[user_key]["history"]:
            data[user_key]["history"].insert(0, session_id)
            data[user_key]["history"] = data[user_key]["history"][:20]
        data[user_key]["active"] = None
        self._sessions_file.write_text(json.dumps(data, indent=2))

    def get_active(self, user_key: str) -> Optional[str]:
        """获取用户的活跃 session_id。"""
        data = self.load()
        return data.get(user_key, {}).get("active")

    def get_history(self, user_key: str) -> list[str]:
        """获取用户的历史 session 列表。"""
        data = self.load()
        return data.get(user_key, {}).get("history", [])

    def get_all_sessions(self, limit: int = 25) -> list[dict]:
        """扫描 project_dir 中所有 .jsonl session 文件。

        Returns:
            按 mtime 降序排列的 [{id, mtime, summary}]，最多 limit 个
        """
        results = []
        try:
            jsonl_files = list(self._project_dir.glob("*.jsonl"))
        except OSError:
            return results

        # 收集 (session_id, mtime) 并按 mtime 降序排序
        entries = []
        for f in jsonl_files:
            sid = f.stem
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            entries.append((sid, mtime))
        entries.sort(key=lambda x: x[1], reverse=True)

        for sid, mtime in entries[:limit]:
            results.append({
                "id": sid,
                "mtime": mtime,
                "summary": self.get_summary(sid),
            })
        return results

    def get_bot_session_ids(self) -> set[str]:
        """返回所有被 bot 管理过的 session_id 集合（active + history）。"""
        data = self.load()
        ids = set()
        for info in data.values():
            if isinstance(info, dict):
                if info.get("active"):
                    ids.add(info["active"])
                ids.update(info.get("history", []))
        return ids

    def get_summary(self, session_id: str) -> str:
        """从 session .jsonl 文件读取第一条用户消息作为摘要。"""
        session_file = self._project_dir / f"{session_id}.jsonl"
        try:
            with open(session_file) as f:
                for line in f:
                    try:
                        d = json.loads(line)
                        if d.get("type") in ("human", "user"):
                            msg = d.get("message", {})
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    p.get("text", "") for p in content
                                    if isinstance(p, dict) and p.get("type") == "text"
                                )
                            # 清理: 去换行、去 [from: XX] 前缀、去 XML 标签
                            text = content.replace("\n", " ").replace("\r", " ")
                            text = re.sub(r"^\[from:\s*\w+\]\s*", "", text)
                            text = re.sub(r"<[^>]+>", "", text)
                            text = " ".join(text.split()).strip()[:80]
                            return text if text else "(empty)"
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        return "(no data)"