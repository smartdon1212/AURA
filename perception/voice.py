"""
Voice capture: records a fixed-duration utterance from the microphone
and packages it as WAV bytes.

Deliberately push-to-talk (a keypress starts a fixed-length recording),
not always-on voice-activity detection. A naive energy-threshold VAD is
prone to false triggers and clipped speech on real hardware and real
room noise — push-to-talk is simpler and completely reliable, which
matters more for v1 correctness than the convenience of hands-free
listening. VAD is a reasonable upgrade later, once this baseline is
proven end to end.

sounddevice chosen over pyaudio deliberately: it ships prebuilt wheels
for Windows (bundles PortAudio), so it installs via `uv sync` with no
system compiler needed — same reasoning that ruled out dlib for face
recognition.
"""

from __future__ import annotations

import io
import wave

import sounddevice as sd

SAMPLE_RATE = 16_000  # Hz — standard for speech models, keeps files small
CHANNELS = 1
SAMPLE_WIDTH_BYTES = 2  # 16-bit PCM


def record_utterance(duration_seconds: float = 5.0) -> bytes:
    """
    Blocks for `duration_seconds`, recording from the default microphone.
    Returns WAV-encoded bytes ready to hand to a TranscriptionProvider.
    """
    frames = sd.rec(
        int(duration_seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="int16",
    )
    sd.wait()  # blocks until recording finishes

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(CHANNELS)
        wav_file.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav_file.setframerate(SAMPLE_RATE)
        wav_file.writeframes(frames.tobytes())

    return buffer.getvalue()
