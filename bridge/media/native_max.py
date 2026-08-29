"""Duration and a waveform for a native voice or circle, read from the file.

MAX's voice (`_type: AUDIO`) and video-note (`_type: VIDEO, videoType: 1`)
attach each carry a `duration` in milliseconds and a `wave` — a byte array of
amplitude bars the client draws under the bubble. Both are read here from the
downloaded media with PyAV, so nothing has to be threaded through the upload
pipeline: the file already holds everything.

The waveform is **7-bit**: every byte is 0..127. The compatibility algorithm is:

  * the recorder measures one amplitude per 20 ms PCM read
    and merges it into an `AtomicInteger` with `max`;
  * a coroutine polls that counter every 75 ms with `getAndSet(0)`, so a tick
    carries the **loudest** of the ~4 reads since the previous one, not the last;
  * the tick maps through a -45 dB floor into a 0..32768 value,
    `val = (int)((max(20*log10(amp/32768), -45) + 45) * 32768/45)`;
  * the byte array is rebuilt on every tick as
    `min(127, (int)(val * min(2.0, 32768/peak) / 256))` — so it is normalised
    against the loudest bar so far, but the scale-up is capped at 2x, which is
    why a quiet recording stays quiet instead of being stretched to full range;
  * on stop the array is resampled to `audio-peaks-count` bars (80 in
    production) with `android.animation.IntEvaluator` interpolation.

A voice and a circle share every step of that; only the amplitude the poll reads
differs, so `kind` picks between the two:

  * a voice comes off `AudioRecord`, and the app takes the chunk's **RMS**,
    `(int) sqrt(sum(s*s) / n)`;
  * a circle comes off CameraX, whose `AudioStats` carries a **peak** amplitude
    normalised to 0..1, which the app multiplies back up by 32768.

The AUDIO validator on the server is stricter than the VIDEO one — a >127
waveform got a circle through but failed a voice with `Invalid media wave`,
which also drops the connection. Hence the 7-bit ceiling here is load-bearing,
not cosmetic.

The poll is wall-clock driven while reads are paced by audio hardware, so burst
edges may move by one bar. Public regression tests use deterministic synthetic
PCM and keep the production builder stable without shipping captured media.
"""

from __future__ import annotations

import array
import logging
import math
from pathlib import Path

logger = logging.getLogger(__name__)

#: Amplitude bars in the waveform — the server-pushed PMS property
#: `audio-peaks-count`, 80 in production (the same count for a 2s and a 10s note).
WAVE_BARS = 80

#: The recorder polls the amplitude on this cadence, so the raw array holds one
#: byte per 75 ms of audio before it is resampled to `WAVE_BARS`.
TICK_MS = 75

#: Window the poll sees — one recorder read, not the whole tick.
CHUNK_MS = 20

#: Everything quieter than this floor is one flat bar.
FLOOR_DB = -45.0

#: Full scale for a 16-bit sample; used both for the dB reference and the byte
#: divisor.
FULL_SCALE = 32768.0

#: The normalisation is capped, so quiet audio is not stretched to full range.
MAX_SCALE = 2.0

#: The waveform is 7-bit — the app clamps every bar here.
WAVE_MAX = 127


def decoder_available() -> bool:
    """Whether anything here can read a file at all.

    Without PyAV every voice and every circle degrades to a plain attachment, for
    every message, silently — which is a different thing from one file this
    install could not decode, and health has to be able to tell them apart. Read
    at snapshot time rather than cached: a decoder that appears after an install
    should stop the alarm without a restart.
    """
    try:
        import av  # noqa: F401
    except Exception:  # noqa: BLE001 - a broken install fails as many ways as a missing one
        return False
    return True


def probe_media(path: Path, kind: str = "voice") -> tuple[int, bytes]:
    """`(duration_ms, wave)` for a voice or circle; `duration_ms` is 0 if unreadable.

    `kind` is `"voice"` or `"circle"` and only picks the amplitude the poll reads
    — RMS for a voice, peak for a circle, matching the two recorders the app uses.

    The waveform is **never empty**: the attach must carry one (an empty or absent
    `wave` is refused), so every failure path falls back to `_silent_wave`. A
    duration of 0 means "could not read", and the caller must not send natively
    with it — the server refuses that too.

    Safe to call from a worker thread — it does blocking decode work and touches
    no event loop.
    """
    try:
        import av  # noqa: F401
    except Exception:
        logger.debug("no decoder available for a native-media waveform", exc_info=True)
        return 0, _silent_wave()
    try:
        return _probe(path, kind)
    except Exception:
        logger.debug("could not read duration/waveform for %s", path.name, exc_info=True)
        return 0, _silent_wave()


class WaveBuilder:
    """The recorder's own accounting, fed PCM in whatever sized pieces arrive.

    Split out of `_probe` so deterministic synthetic PCM can exercise the same
    builder production uses. Reimplementing the tick loop in tests would only
    prove that two copies agree.

    `chunk_samples` is one recorder read and `tick_samples` is one poll. Both are
    derived from the rate rather than fixed, because the stand records at 48 kHz
    and the decode path resamples to 16 kHz — the timing is what matters, not the
    sample count.
    """

    def __init__(self, rate: int, *, peak: bool) -> None:
        self._chunk = max(1, rate * CHUNK_MS // 1000)
        self._step = max(1, rate * TICK_MS // 1000)
        self._peak = peak
        self._buffer = array.array("h")
        self._values: list[int] = []
        self._total = 0  # samples measured overall, against which ticks fire
        self._next_tick = self._step
        self._loudest = 0  # the running max a tick reads and clears

    def feed(self, samples: array.array[int]) -> None:
        """Measure everything whole reads can be taken from; keep the remainder.

        Chunking is against a running total rather than against this call, so
        feeding one buffer of a thousand samples and a thousand buffers of one
        produce the same bars.
        """
        self._buffer.extend(samples)
        consumed = 0
        while len(self._buffer) - consumed >= self._chunk:
            # One amplitude per `chunk` (a recorder read), merged with `max`;
            # a tick every `step` takes that maximum and clears it.
            self._loudest = max(
                self._loudest, _amplitude(self._buffer, consumed, self._chunk, self._peak)
            )
            consumed += self._chunk
            self._total += self._chunk
            while self._total >= self._next_tick:
                self._values.append(_tick_value(self._loudest))
                self._loudest = 0
                self._next_tick += self._step
        del self._buffer[:consumed]

    def finish(self) -> bytes | None:
        """The 80 bars, or None when nothing was long enough to measure."""
        if self._loudest:  # a partial tick at the end still carries its loudest read
            self._values.append(_tick_value(self._loudest))
        if not self._values:
            return None
        return _resample(WAVE_BARS, _raw_wave(self._values))


def _probe(path: Path, kind: str) -> tuple[int, bytes]:
    import av
    from av.audio.resampler import AudioResampler

    duration_ms = 0
    rate = 16000
    resampler = AudioResampler(format="s16", layout="mono", rate=rate)
    builder = WaveBuilder(rate, peak=kind == "circle")
    samples = array.array("h")

    with av.open(str(path)) as container:
        if container.duration:  # AV_TIME_BASE is microseconds
            duration_ms = int(container.duration / 1000)
        audio = container.streams.audio
        if not audio:
            # A circle recorded with the mic off is exactly this, and the app
            # sends it as a full run of zero bars: CameraX reports
            # AUDIO_STATE_DISABLED, the poll reads an amplitude of 0 on every
            # tick, and the -45 dB floor turns all of them into 0.
            return duration_ms, _silent_wave()
        stream = audio[0]
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                # A plane's buffer is padded past the samples it holds, and the
                # padding is uninitialised (reads as loud garbage) — take only
                # the valid samples*2 bytes, or every bar comes out clipped.
                del samples[:]
                samples.frombytes(bytes(resampled.planes[0])[: resampled.samples * 2])
                builder.feed(samples)
        if duration_ms == 0 and stream.duration and stream.time_base:
            duration_ms = int(float(stream.duration * stream.time_base) * 1000)

    wave = builder.finish()
    if wave is None:
        # Nothing decoded — a clip shorter than one tick, or a codec-odd track.
        # An empty waveform is refused, so send the same all-zero one a soundless
        # circle carries rather than fail on a field the user never sees.
        return duration_ms, _silent_wave()
    return duration_ms, wave


def _amplitude(samples: array.array[int], start: int, chunk: int, peak: bool) -> int:
    """One recorder read: `peak` picks a circle's amplitude (CameraX reports the
    loudest sample) over a voice's (AudioRecord's RMS)."""
    window = samples[start : start + chunk]
    if not window:
        return 0
    if peak:
        return max(abs(s) for s in window)
    return int(math.sqrt(sum(s * s for s in window) / len(window)))


def _tick_value(amplitude: int) -> int:
    """One poll tick: the loudest read since the last tick, as a 0..32768 value."""
    db = FLOOR_DB if amplitude == 0 else 20.0 * math.log10(amplitude / FULL_SCALE)
    if db < FLOOR_DB:
        db = FLOOR_DB
    return int((db - FLOOR_DB) * FULL_SCALE / -FLOOR_DB)


def _raw_wave(values: list[int]) -> bytes:
    """The byte array MAX keeps while recording — one byte per `TICK_MS`."""
    peak = max(values)
    scale = min(MAX_SCALE, FULL_SCALE / peak) if peak else MAX_SCALE
    return bytes(min(WAVE_MAX, int(v * scale / 256.0)) for v in values)


def _silent_wave() -> bytes:
    """The waveform the app sends for a circle recorded without sound: `WAVE_BARS`
    bars, all zero. Non-empty, so it never trips the empty-wave refusal."""
    return bytes(WAVE_BARS)


def _resample(count: int, src: bytes) -> bytes:
    """Resample `src` to `count` bars the way the app does (`qb0.c`).

    Interpolation is `android.animation.IntEvaluator`, and the bounds check is
    against `len - 1` rather than `len`, so the bars that land on the final
    source pair come out as 0 — a quirk of the original that real waveforms
    carry, so it is reproduced rather than fixed.
    """
    if not src:
        return src
    out = bytearray(count)
    for j in range(count):
        if j == 0 or len(src) == 1:
            value = src[0]
        elif j == count - 1:
            value = src[-1]
        else:
            x = (j / count) * (len(src) - 1)
            low = int(x)
            high = low + 1
            if low >= len(src) - 1 or high >= len(src) - 1:
                value = 0
            else:
                value = int(src[low] + (x - low) * (src[high] - src[low]))
        out[j] = value & 0xFF
    return bytes(out)
