# Copyright 2025-2026 Chris Yang (yangwhale)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Chirp 3 STT adapter via Google Cloud Speech v2 (Vertex AI region).

Alternative to GeminiSTT for users who want a STT-specialized model rather
than a multimodal LLM. Chirp 3 is Google's latest USM-based model, optimized
for Mandarin + 100+ languages with low latency.

Benchmarked vs GeminiSTT on synthesized TTS audio (7 Mandarin utterances,
1–9 seconds): equal or higher accuracy with 3–5x lower and far more stable
latency (Chirp 1.1–1.6s vs Gemini 1.7–11.7s end-to-end).

Region note (sharp edge): Chirp 3 + Mandarin (`cmn-Hans-CN`) is currently
only available in `asia-southeast1`. Not in `global`, not in `us-*`. Other
languages may have different availability. Stick with the default unless
you know your language is in another region.

Uses the same Vertex AI service account credentials as GeminiSTT — no
extra API key needed, no new dependencies (google-cloud-speech is already
present transitively).

Selection: set Firestore `bots/{name}.livekit.stt_provider = "chirp3"`
(default "gemini" keeps GeminiSTT behavior unchanged).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from google.api_core import client_options as gapic_options
from google.cloud import speech_v2
from google.cloud.speech_v2.types import cloud_speech

from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, stt, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr

from .chirp_phrases import default_phrases

log = logging.getLogger("closecrab.voice.chirp_stt")

# Cloud Speech caps boost in [0, 20]. 10 is the documented sweet spot for
# strong-but-not-trigger-happy biasing; per-phrase overrides bump proper
# nouns higher (see chirp_phrases.py).
_DEFAULT_PHRASE_BOOST = 10.0


def _build_speech_client(location: str) -> speech_v2.SpeechAsyncClient:
    # Speech v2 needs a regional endpoint for non-global locations. "global"
    # uses the default endpoint; specific regions (us-central1, asia-southeast1)
    # need an explicit api_endpoint override or the request fails with
    # "INVALID_ARGUMENT: Recognizer in invalid location".
    if location and location != "global":
        opts = gapic_options.ClientOptions(api_endpoint=f"{location}-speech.googleapis.com")
        return speech_v2.SpeechAsyncClient(client_options=opts)
    return speech_v2.SpeechAsyncClient()


class ChirpSTT(stt.STT):
    def __init__(
        self,
        *,
        model: str = "chirp_3",
        language: str = "cmn-Hans-CN",
        project: str | None = None,
        location: str = "asia-southeast1",
        phrases: list[tuple[str, float | None]] | list[str] | None = None,
        phrase_boost: float = _DEFAULT_PHRASE_BOOST,
    ) -> None:
        """
        Args:
            phrases: Vocabulary biasing list. Either ["Gemini", "Claude", ...]
                (all phrases share `phrase_boost`) or [("Higcp", 18.0),
                ("Gemini", None), ...] (per-phrase override; None falls back
                to `phrase_boost`). None disables adaptation entirely; pass
                `chirp_phrases.default_phrases()` to use the built-in list.
            phrase_boost: PhraseSet-level boost in (0, 20]. Per-phrase
                overrides win. 10 is a good starting point.
        """
        super().__init__(
            capabilities=stt.STTCapabilities(streaming=False, interim_results=False),
        )
        self._model = model
        self._language = language
        self._project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not self._project:
            raise ValueError(
                "ChirpSTT requires GOOGLE_CLOUD_PROJECT env var or project= arg"
            )
        self._location = location
        # Speech v2 expects a "recognizer" resource path. Using the "_" wildcard
        # recognizer skips per-project recognizer setup — config travels with
        # each request via recognition_config. Best for ad-hoc usage.
        self._recognizer = f"projects/{self._project}/locations/{location}/recognizers/_"
        self._client = _build_speech_client(location)
        self._adaptation = self._build_adaptation(phrases, phrase_boost)

    @staticmethod
    def _build_adaptation(
        phrases: list[tuple[str, float | None]] | list[str] | None,
        default_boost: float,
    ) -> cloud_speech.SpeechAdaptation | None:
        if not phrases:
            return None
        proto_phrases = []
        for entry in phrases:
            if isinstance(entry, tuple):
                value, boost_override = entry
            else:
                value, boost_override = entry, None
            # Per-phrase boost=0 means "use PhraseSet default" in the API,
            # so we leave it unset when no override is given.
            kwargs = {"value": value}
            if boost_override is not None:
                kwargs["boost"] = float(boost_override)
            proto_phrases.append(cloud_speech.PhraseSet.Phrase(**kwargs))
        return cloud_speech.SpeechAdaptation(
            phrase_sets=[
                cloud_speech.SpeechAdaptation.AdaptationPhraseSet(
                    inline_phrase_set=cloud_speech.PhraseSet(
                        phrases=proto_phrases,
                        boost=default_boost,
                    )
                )
            ]
        )

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return "chirp"

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        # Chirp accepts raw LINEAR16 PCM directly via ExplicitDecodingConfig —
        # no WAV header wrapping needed, saves a few bytes per request.
        pcm = frame.data.tobytes()
        lang_tag: Any = language if language else self._language

        config_kwargs: dict[str, Any] = dict(
            explicit_decoding_config=cloud_speech.ExplicitDecodingConfig(
                encoding=cloud_speech.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=frame.sample_rate,
                audio_channel_count=frame.num_channels,
            ),
            language_codes=[lang_tag],
            model=self._model,
            features=cloud_speech.RecognitionFeatures(
                # Punctuation on so transcripts feel natural going into the LLM.
                enable_automatic_punctuation=True,
            ),
        )
        if self._adaptation is not None:
            config_kwargs["adaptation"] = self._adaptation
        config = cloud_speech.RecognitionConfig(**config_kwargs)
        request = cloud_speech.RecognizeRequest(
            recognizer=self._recognizer,
            config=config,
            content=pcm,
        )

        try:
            response = await self._client.recognize(request=request)
        except Exception as e:
            log.warning("ChirpSTT recognize failed: %s", e)
            raise APIConnectionError() from e

        # Concatenate transcripts from all returned results (usually 1 for a
        # single utterance, but Chirp may segment longer audio into several).
        text_parts = []
        for result in response.results:
            if result.alternatives:
                text_parts.append(result.alternatives[0].transcript)
        text = " ".join(text_parts).strip()

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=lang_tag, text=text)],
        )
