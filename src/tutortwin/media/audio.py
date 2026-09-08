"""Voice notes: entitlement, validation, transcription.

Voice is exempt from the brief gate for a reason that is about meaning, not
leniency: a voice note *is* the request, so there is nothing to ask the student
to clarify. Everything else still applies - entitlement is checked before a
single second is transcribed, and duration and size are capped.

The transcript then re-enters the ordinary text pipeline, so a spoken question
routes, budgets and answers exactly like a typed one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Protocol

from tutortwin.domain.media import MediaLimits, RejectReason
from tutortwin.domain.provider import (
    ErrorCategory,
    ModelAlias,
    ModelCall,
    Provider,
    StopReason,
)
from tutortwin.observability.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    duration_seconds: float | None
    call: ModelCall | None
    """None when no paid call was made - the counter tests assert on this."""


class TranscriptionProvider(Protocol):
    async def transcribe(
        self, audio: bytes, *, mime_type: str, model_id: str
    ) -> TranscriptionResult: ...


@dataclass(frozen=True, slots=True)
class AudioCheck:
    ok: bool
    duration_seconds: float | None = None
    reason: RejectReason | None = None
    detail: str = ""


def probe_duration(data: bytes, mime_type: str) -> float | None:
    """Best-effort duration without decoding the stream.

    Only WAV is parsed exactly - its header states the rate and byte count.
    Compressed formats return None, and the size cap does the bounding instead;
    decoding them locally would need a codec dependency this phase does not
    justify.
    """
    if mime_type not in {"audio/wav", "audio/x-wav"} or len(data) < 44:
        return None
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        # Walk the chunk list rather than assuming a canonical 44-byte header.
        offset = 12
        byte_rate = 0
        while offset + 8 <= len(data):
            chunk_id = data[offset : offset + 4]
            (chunk_size,) = struct.unpack("<I", data[offset + 4 : offset + 8])
            if chunk_id == b"fmt " and offset + 8 + 16 <= len(data):
                (byte_rate,) = struct.unpack("<I", data[offset + 16 : offset + 20])
            elif chunk_id == b"data" and byte_rate:
                return float(chunk_size) / float(byte_rate)
            offset += 8 + chunk_size + (chunk_size % 2)
    except (struct.error, ZeroDivisionError):
        return None
    return None


def check_audio(data: bytes, *, mime_type: str, limits: MediaLimits) -> AudioCheck:
    """Size and duration caps, before any transcription is paid for."""
    if len(data) > limits.max_audio_bytes:
        return AudioCheck(
            False,
            reason=RejectReason.TOO_LARGE,
            detail=f"{len(data)} bytes exceeds {limits.max_audio_bytes}",
        )

    duration = probe_duration(data, mime_type)
    if duration is not None and duration > limits.max_audio_seconds:
        return AudioCheck(
            False,
            duration_seconds=duration,
            reason=RejectReason.TOO_LONG,
            detail=f"{duration:.0f}s exceeds {limits.max_audio_seconds}s",
        )
    return AudioCheck(True, duration_seconds=duration)


class VoicePipeline:
    def __init__(
        self,
        *,
        transcriber: TranscriptionProvider | None,
        limits: MediaLimits,
    ) -> None:
        self._transcriber = transcriber
        self._limits = limits

    async def transcribe(
        self,
        data: bytes,
        *,
        mime_type: str,
        entitled: bool,
        model_id: str = "whisper-1",
    ) -> TranscriptionResult:
        """Entitlement first. An ineligible student costs zero transcription."""
        if not entitled:
            logger.info("transcription_refused_entitlement", transcriptions=0)
            return TranscriptionResult(text="", duration_seconds=None, call=None)

        check = check_audio(data, mime_type=mime_type, limits=self._limits)
        if not check.ok:
            logger.info(
                "audio_rejected",
                reason=check.reason.value if check.reason else None,
                transcriptions=0,
            )
            return TranscriptionResult(text="", duration_seconds=check.duration_seconds, call=None)

        if self._transcriber is None:
            logger.info("transcription_unavailable", transcriptions=0)
            return TranscriptionResult(text="", duration_seconds=check.duration_seconds, call=None)

        return await self._transcriber.transcribe(data, mime_type=mime_type, model_id=model_id)


class OpenAITranscriptionProvider:
    """OpenAI audio transcription. The only vendor path for speech in Phase 03."""

    def __init__(self, api_key: str) -> None:
        import openai

        self._client = openai.AsyncOpenAI(api_key=api_key)

    async def transcribe(
        self, audio: bytes, *, mime_type: str, model_id: str
    ) -> TranscriptionResult:
        import io
        import time

        import openai

        started = time.monotonic()
        buffer = io.BytesIO(audio)
        buffer.name = f"audio.{mime_type.split('/')[-1]}"

        try:
            response = await self._client.audio.transcriptions.create(model=model_id, file=buffer)
            text = response.text
            error = ErrorCategory.NONE
            stop = StopReason.END_TURN
            message = None
        except openai.APITimeoutError as exc:
            text, error, stop, message = "", ErrorCategory.TIMEOUT, StopReason.ERROR, str(exc)
        except openai.RateLimitError as exc:
            text, error, stop, message = (
                "",
                ErrorCategory.RATE_LIMIT,
                StopReason.ERROR,
                str(exc),
            )
        except openai.AuthenticationError as exc:
            text, error, stop, message = "", ErrorCategory.AUTH, StopReason.ERROR, str(exc)
        except openai.APIStatusError as exc:
            category = (
                ErrorCategory.SERVER_ERROR if exc.status_code >= 500 else ErrorCategory.BAD_REQUEST
            )
            text, error, stop, message = "", category, StopReason.ERROR, str(exc)

        # Transcription is billed by audio duration, not tokens. The ledger row
        # records the call so it is visible in spend even though the token
        # columns stay zero.
        call = ModelCall(
            alias=ModelAlias.TRANSCRIBE,
            provider=Provider.OPENAI,
            model_id=model_id,
            text=text,
            latency_ms=int((time.monotonic() - started) * 1000),
            stop_reason=stop,
            error_category=error,
            error_message=message[:200] if message else None,
        )
        return TranscriptionResult(
            text=text, duration_seconds=probe_duration(audio, mime_type), call=call
        )
