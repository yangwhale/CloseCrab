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

from abc import ABC, abstractmethod
from typing import Optional, Callable, Awaitable


class Worker(ABC):
    @abstractmethod
    async def start(self, session_id: Optional[str] = None) -> str:
        """启动 worker，返回 session_id"""
        ...

    @abstractmethod
    async def send(self, text: str, on_event: Optional[Callable[[str], Awaitable[None]]] = None) -> str:
        """发送消息等响应"""
        ...

    @abstractmethod
    async def stop(self):
        ...

    @abstractmethod
    async def interrupt(self) -> bool:
        """中断当前执行，保留 session"""
        ...

    @abstractmethod
    def is_alive(self) -> bool:
        ...

    @property
    @abstractmethod
    def is_busy(self) -> bool:
        ...

    @property
    @abstractmethod
    def session_id(self) -> Optional[str]:
        ...

    @abstractmethod
    def get_context_usage(self) -> dict:
        """返回 context/token 用量"""
        ...