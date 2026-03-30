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

from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class UnifiedMessage:
    channel_type: str       # "discord" / "feishu"
    user_id: str
    content: str            # 纯文字（语音已在 Channel 层转好）
    reply: Callable[[str], Awaitable[None]]  # 回复方法
    metadata: dict = field(default_factory=dict)