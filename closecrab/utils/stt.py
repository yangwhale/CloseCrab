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

"""Speech-to-Text engine abstraction.

Extracted from bot.py, supports Gemini / Chirp2 / Whisper with automatic fallback.
No Discord or Channel dependencies.
"""

import logging
from pathlib import Path

log = logging.getLogger("closecrab.utils.stt")

# Lazy-loaded whisper model singleton
_whisper_model = None


class STTEngine:
    """语音转文字引擎，支持 Gemini / Chirp2 / Whisper 三种后端 + fallback chain。

    Args:
        engine: 引擎选择 - "gemini" (默认) / "chirp2" / "whisper" / "whisper:large" 等
        project: GCP project ID (Chirp2 / Gemini 需要)
        location: GCP location (Chirp2 需要，Gemini 固定用 global)
        whisper_model: Whisper 模型大小，默认 "medium"
    """

    def __init__(
        self,
        engine: str = "gemini",
        project: str = None,
        location: str = "us-central1",
        whisper_model: str = "medium",
    ):
        self._engine = engine
        if project is None:
            from ..constants import G
            project = G.GCP_PROJECT
        self._project = project
        self._location = location
        # whisper:large 格式支持
        if engine.startswith("whisper:") and ":" in engine:
            self._whisper_model_name = engine.split(":", 1)[1]
        else:
            self._whisper_model_name = whisper_model

    def transcribe(self, file_path: str) -> str:
        """根据配置选择引擎转写，含 fallback chain。

        Fallback 顺序: 主引擎 -> Chirp2 -> Whisper (gemini 主引擎时)
                       主引擎 -> Whisper (chirp2 主引擎时)
        """
        try:
            if self._engine.startswith("whisper"):
                return self._transcribe_whisper(file_path)
            elif self._engine == "gemini":
                return self._transcribe_gemini(file_path)
            else:
                return self._transcribe_chirp2(file_path)
        except Exception as e:
            log.warning(f"Transcription failed ({self._engine}): {e}")
            # fallback chain
            if self._engine == "gemini":
                log.info("Falling back to Chirp 2...")
                try:
                    return self._transcribe_chirp2(file_path)
                except Exception as e2:
                    log.warning(f"Chirp 2 fallback also failed: {e2}")
            if not self._engine.startswith("whisper"):
                log.info("Falling back to Whisper...")
                try:
                    return self._transcribe_whisper(file_path)
                except Exception as e2:
                    log.warning(f"Whisper fallback also failed: {e2}")
            return ""

    def _transcribe_whisper(self, file_path: str) -> str:
        """用本地 Whisper 模型转写音频文件。"""
        global _whisper_model
        import whisper

        if _whisper_model is None:
            log.info(f"Loading Whisper {self._whisper_model_name} model (first time)...")
            _whisper_model = whisper.load_model(self._whisper_model_name)
            log.info("Whisper model loaded.")

        result = _whisper_model.transcribe(file_path, language=None)
        text = result.get("text", "").strip()
        lang = result.get("language", "unknown")
        log.info(f"Whisper transcribed ({lang}): {text[:100]}...")
        return text

    def _transcribe_chirp2(self, file_path: str) -> str:
        """用 Google Cloud Speech-to-Text v2 (Chirp 2) 转写音频文件。"""
        from google.cloud.speech_v2 import SpeechClient
        from google.cloud.speech_v2.types import cloud_speech

        client = SpeechClient(
            client_options={"api_endpoint": f"{self._location}-speech.googleapis.com"}
        )

        with open(file_path, "rb") as f:
            audio_content = f.read()

        config = cloud_speech.RecognitionConfig(
            auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
            language_codes=["cmn-Hans-CN"],
            model="chirp_2",
            denoiser_config=cloud_speech.DenoiserConfig(
                denoise_audio=True,
                snr_threshold=10.0,
            ),
            adaptation=cloud_speech.SpeechAdaptation(
                phrase_sets=[
                    cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                        inline_phrase_set=cloud_speech.PhraseSet(phrases=[
                            {"value": "Claude Code"},
                            {"value": "Chirp"},
                            {"value": "Whisper"},
                            {"value": "TPU"},
                            {"value": "GPU"},
                            {"value": "B200"},
                            {"value": "H100"},
                            {"value": "A100"},
                            {"value": "SGLang"},
                            {"value": "vLLM"},
                            {"value": "GCP"},
                            {"value": "Discord"},
                            {"value": "Gemini"},
                            {"value": "sglang"},
                            {"value": "MIG"},
                            {"value": "GKE"},
                            {"value": "Spot"},
                            {"value": "HBM"},
                        ])
                    )
                ]
            ),
        )
        request = cloud_speech.RecognizeRequest(
            recognizer=f"projects/{self._project}/locations/{self._location}/recognizers/_",
            config=config,
            content=audio_content,
        )

        response = client.recognize(request=request)
        texts = []
        for result in response.results:
            if result.alternatives:
                texts.append(result.alternatives[0].transcript)
        text = " ".join(texts).strip()
        log.info(f"Chirp 2 transcribed: {text[:100]}...")
        return text

    def _transcribe_gemini(self, file_path: str) -> str:
        """用 Gemini 3 Flash 多模态模型转写音频文件（理解语义，自动纠正同音字）。"""
        from google import genai
        from google.genai import types

        client = genai.Client(
            vertexai=True,
            project=self._project,
            location="global",
        )

        with open(file_path, "rb") as f:
            audio_bytes = f.read()

        # 检测 mime type
        suffix = Path(file_path).suffix.lower()
        mime_map = {
            ".ogg": "audio/ogg",
            ".wav": "audio/wav",
            ".mp3": "audio/mp3",
            ".m4a": "audio/mp4",
            ".webm": "audio/webm",
            ".flac": "audio/flac",
        }
        mime_type = mime_map.get(suffix, "audio/ogg")

        prompt = (
            "请将这段语音精确转录为简体中文文字。"
            "只输出转录文字，不要加任何解释、标点符号说明或额外内容。"
            "如果语音中包含英文技术术语（如 GPU、TPU、Claude Code 等），保留英文原文。"
        )
        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type=mime_type)
        text_part = types.Part.from_text(text=prompt)
        response = client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=[
                types.Content(
                    parts=[audio_part, text_part],
                    role="user",
                )
            ],
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="MINIMAL"),
            ),
        )

        text = response.text.strip()
        log.info(f"Gemini transcribed: {text[:100]}...")
        return text