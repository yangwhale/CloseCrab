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

"""Authentication module."""

import logging

log = logging.getLogger("closecrab.core.auth")


class Auth:
    """用户鉴权，基于白名单。

    Args:
        allowed_user_ids: 允许的用户 ID 集合。空集合表示允许所有用户。
    """

    def __init__(self, allowed_user_ids: set[int] = None):
        self._allowed = allowed_user_ids or set()

    def is_allowed(self, user_id: int) -> bool:
        if not self._allowed:
            return True
        return user_id in self._allowed