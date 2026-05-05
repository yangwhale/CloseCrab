# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""LiveKit voice IO 模块。

提供 voice 作为飞书 channel 的语音 IO 模式：
- STT/TTS 适配器（Gemini）
- LiveKit Worker 包装（LiveKitVoiceIO）
- CloseCrab LLM plugin（路由到飞书 worker）

详见 docs/livekit-voice-channel-design.md。

注意: 此模块依赖 livekit-agents / livekit-plugins-* / google-genai 等。
为避免非 voice bot 强制装这些依赖, 不在顶层 import。
使用方式: `from closecrab.voice.livekit_io import LiveKitVoiceIO`
"""
