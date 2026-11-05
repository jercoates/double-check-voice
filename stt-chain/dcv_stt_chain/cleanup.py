"""Cheap, dependency-free audio clean-up for 16 kHz, 16-bit, mono PCM.

Presets (what a profile's `cleanup` names):
  none   untouched (the default: what a device with no profile gets)
  light  high-pass 80 Hz, then raise the level so the peak reaches -3 dBFS (at most +12 dB)
  boost  high-pass 150 Hz, then the same levelling with up to +30 dB

These are level and rumble fixes, not noise reduction. Whether they help is for the
replay A/B to show; the raw audio is always saved before any of this runs.
"""

import math
from array import array

PRESETS = {
    "none": None,
    "light": {"highpass_hz": 80.0, "peak_dbfs": -3.0, "max_gain_db": 12.0},
    "boost": {"highpass_hz": 150.0, "peak_dbfs": -3.0, "max_gain_db": 30.0},
}


def highpass(samples: array, rate: int, cutoff_hz: float) -> array:
    """Second-order Butterworth high-pass (biquad). Returns a new array of int16.

    A rate or cutoff that makes no sense (rate <= 0, cutoff outside 0 .. rate/2) returns
    the samples unchanged. The filter starts as if the first sample had always been
    there, so a clip that does not begin at zero gets no start-up step.
    """
    if rate <= 0 or not (0.0 < cutoff_hz < rate / 2.0) or not samples:
        return samples
    w0 = 2.0 * math.pi * cutoff_hz / rate
    alpha = math.sin(w0) / math.sqrt(2.0)  # Q = 1/sqrt(2)
    cos_w0 = math.cos(w0)
    b0 = (1.0 + cos_w0) / 2.0
    b1 = -(1.0 + cos_w0)
    b2 = b0
    a0 = 1.0 + alpha
    a1 = -2.0 * cos_w0
    a2 = 1.0 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    out = array("h", bytes(len(samples) * 2))
    x1 = x2 = float(samples[0])
    y1 = y2 = 0.0
    for i, x in enumerate(samples):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x
        y2, y1 = y1, y
        out[i] = 32767 if y > 32767 else -32768 if y < -32768 else int(y)
    return out


def level(samples: array, peak_dbfs: float, max_gain_db: float) -> array:
    """Scale so the loudest sample reaches peak_dbfs, never more than max_gain_db, never down."""
    peak = max((abs(s) for s in samples), default=0)
    if peak == 0:
        return samples
    target = 32767 * (10.0 ** (peak_dbfs / 20.0))
    gain = min(target / peak, 10.0 ** (max_gain_db / 20.0))
    if gain <= 1.0:
        return samples  # already loud enough: never turn it down
    out = array("h", bytes(len(samples) * 2))
    for i, s in enumerate(samples):
        v = int(s * gain)
        out[i] = 32767 if v > 32767 else -32768 if v < -32768 else v
    return out


def apply(pcm: bytes, preset: str, rate: int = 16000) -> bytes:
    """Apply a named preset to 16-bit little-endian mono PCM.

    An unknown preset name is a ValueError (a settings mistake, caught at start-up).
    Anything else going wrong returns the audio untouched: clean-up must never break a
    run. A stray odd byte at the end is passed through unchanged, so the length is kept.

    Runs in plain Python and blocks while it works (about 0.6 s for 30 s of audio on a
    fast PC): callers on an event loop must run it in an executor.
    """
    if preset not in PRESETS:
        raise ValueError(f"unknown cleanup preset {preset!r} (use {', '.join(PRESETS)})")
    spec = PRESETS[preset]
    if spec is None or len(pcm) < 2:
        return pcm
    try:
        whole = len(pcm) - (len(pcm) % 2)
        samples = array("h")
        samples.frombytes(pcm[:whole])
        if _big_endian():
            samples.byteswap()
        filtered = highpass(samples, rate, spec["highpass_hz"])
        if filtered is samples:
            return pcm  # the filter was refused (nonsense rate): leave the audio alone
        samples = level(filtered, spec["peak_dbfs"], spec["max_gain_db"])
        if _big_endian():
            samples.byteswap()
        return samples.tobytes() + pcm[whole:]
    except Exception:  # noqa: BLE001 - never break a run over clean-up
        return pcm


def _big_endian() -> bool:
    import sys

    return sys.byteorder == "big"
