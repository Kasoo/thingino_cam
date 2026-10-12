"""Decode any audio file to the mono signed 16-bit PCM the backchannel sends, with PyAV.

No Home Assistant imports. Blocking: call from an executor.
"""
from __future__ import annotations

import io
from collections.abc import Iterator

import av

MAX_SECONDS = 120
MAX_BYTES = 10 * 1024 * 1024
RATES = (16000, 8000)       # L16 (Raptor) and PCMU (prudynt)


class AudioError(Exception):
    """The media can't be played: not audio, unreadable, or too long."""


class AudioTooLong(AudioError):
    """Longer than the clip limit."""


def decode(data: bytes, max_seconds: float | None = None) -> tuple[bytes, bytes]:
    """(16 kHz, 8 kHz) native-endian signed 16-bit mono PCM of the first audio stream in `data`.

    Both rates come from one decoding pass, each with its own resampler, so 8 kHz isn't made by
    dropping samples."""
    max_seconds = max_seconds or MAX_SECONDS
    resamplers = [av.AudioResampler(format="s16", layout="mono", rate=rate) for rate in RATES]
    out = [bytearray() for _ in RATES]
    limit = int(max_seconds * RATES[0]) * 2
    try:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.audio:
                raise AudioError("the media has no audio")
            for frame in _frames(container):
                for resampler, pcm in zip(resamplers, out):
                    for chunk in resampler.resample(frame):
                        pcm += bytes(chunk.planes[0])[: chunk.samples * 2]
                if len(out[0]) > limit:
                    raise AudioTooLong(f"the media is longer than {max_seconds:.0f} seconds")
    except av.FFmpegError as err:
        raise AudioError(f"can't decode the media: {err}") from err
    if not out[0]:
        raise AudioError("the media has no audio")
    return bytes(out[0]), bytes(out[1])


def _frames(container: av.container.InputContainer) -> Iterator[av.AudioFrame | None]:
    yield from container.decode(audio=0)
    yield None          # flushes the resamplers
