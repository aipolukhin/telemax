"""Privacy-safe regression coverage for the MAX waveform builder.

The tests build deterministic square-wave PCM in memory and exercise the real
PyAV decode path without shipping anybody's media.
"""

from __future__ import annotations

import array
import sys
import wave
from pathlib import Path

from bridge.media.native_max import WAVE_BARS, WAVE_MAX, WaveBuilder

RATE = 48_000
CHUNK_SAMPLES = RATE * 20 // 1000
EXPECTED_WAVE = bytes.fromhex(
    "000000000000434949494949494949494949100000506d6d6d6d6d6d6d6d6d6d6d6d6d6d3100000000"
    "3d7f7f7f7f7f7f7f7f4f0000205f5f5f5f5f5f5f5f5f5f5f5f5f4f000000000000000000000000"
)


def _synthetic_pcm() -> array.array[int]:
    """Six seconds of exact, non-biometric square-wave bursts and silence."""
    amplitudes = (
        [0] * 25
        + [3_000] * 40
        + [0] * 15
        + [12_000] * 55
        + [0] * 20
        + [24_000] * 30
        + [0] * 15
        + [7_000] * 50
        + [0] * 50
    )
    samples = array.array("h")
    for amplitude in amplitudes:
        samples.extend(
            amplitude if index % 2 == 0 else -amplitude for index in range(CHUNK_SAMPLES)
        )
    return samples


def _production_wave(samples: array.array[int]) -> bytes:
    builder = WaveBuilder(RATE, peak=False)
    builder.feed(samples)
    wave = builder.finish()
    assert wave is not None
    return wave


def test_synthetic_waveform_snapshot() -> None:
    wave = _production_wave(_synthetic_pcm())
    assert wave == EXPECTED_WAVE
    assert len(wave) == WAVE_BARS
    assert max(wave) <= WAVE_MAX


def test_feeding_the_same_pcm_in_pieces_changes_nothing() -> None:
    samples = _synthetic_pcm()
    whole = _production_wave(samples)

    piecemeal = WaveBuilder(RATE, peak=False)
    for start in range(0, len(samples), 517):  # deliberately not a chunk multiple
        piecemeal.feed(samples[start : start + 517])

    assert piecemeal.finish() == whole


def test_a_synthetic_file_decodes_to_a_sendable_waveform(tmp_path: Path) -> None:
    """Exercise the real PyAV path without committing a captured voice file."""
    from bridge.media.native_max import probe_media

    samples = _synthetic_pcm()
    little_endian = array.array("h", samples)
    if sys.byteorder != "little":
        little_endian.byteswap()
    source = tmp_path / "synthetic.wav"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(little_endian.tobytes())

    duration_ms, waveform = probe_media(source, "voice")

    assert duration_ms > 0
    assert len(waveform) == WAVE_BARS
    assert max(waveform) <= WAVE_MAX
    assert probe_media(source, "voice") == (duration_ms, waveform)
