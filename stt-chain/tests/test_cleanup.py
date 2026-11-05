"""Clean-up presets: each claim checked on synthetic signals."""

import math
import time
from array import array

import pytest

from dcv_stt_chain import cleanup

RATE = 16000


def tone(freq, seconds=0.5, amp=8000):
    n = int(RATE * seconds)
    return array("h", [int(amp * math.sin(2 * math.pi * freq * i / RATE)) for i in range(n)])


def rms(samples, skip=2000):
    body = samples[skip:]  # skip the filter's start-up
    return math.sqrt(sum(s * s for s in body) / len(body))


def to_bytes(samples):
    return samples.tobytes()


def from_bytes(data):
    a = array("h")
    a.frombytes(data)
    return a


def test_none_returns_the_same_bytes():
    pcm = to_bytes(tone(440))
    assert cleanup.apply(pcm, "none") is pcm


def test_unknown_preset_is_an_error():
    with pytest.raises(ValueError):
        cleanup.apply(b"\x00\x00", "heavy")


def test_highpass_cuts_rumble_and_keeps_speech_band():
    low = tone(30)
    mid = tone(1000)
    low_out = cleanup.highpass(low, RATE, 150.0)
    mid_out = cleanup.highpass(mid, RATE, 150.0)
    assert rms(low_out) < 0.1 * rms(low)  # 30 Hz is at least 20 dB down
    assert rms(mid_out) > 0.95 * rms(mid)  # 1 kHz is essentially untouched


def test_level_raises_quiet_audio_to_the_target_peak_and_caps_the_gain():
    quiet = tone(500, amp=1000)  # peak 1000
    out = cleanup.level(quiet, peak_dbfs=-3.0, max_gain_db=40.0)
    target = 32767 * 10 ** (-3.0 / 20)
    assert abs(max(abs(s) for s in out) - target) < 0.02 * target
    capped = cleanup.level(quiet, peak_dbfs=-3.0, max_gain_db=6.0)  # 6 dB = x2 at most
    assert max(abs(s) for s in capped) <= 2.01 * 1000


def test_level_never_turns_loud_audio_down_and_handles_silence():
    loud = tone(500, amp=32000)
    assert cleanup.level(loud, -3.0, 12.0) is loud
    silence = array("h", [0] * 1000)
    assert cleanup.level(silence, -3.0, 12.0) is silence


def test_apply_presets_change_audio_and_keep_length():
    pcm = to_bytes(tone(500, amp=800))
    for name in ("light", "boost"):
        out = cleanup.apply(pcm, name)
        assert len(out) == len(pcm) and out != pcm
        assert max(abs(s) for s in from_bytes(out)) > max(abs(s) for s in from_bytes(pcm))


def test_odd_length_empty_and_clipping_input_do_not_crash():
    assert cleanup.apply(b"", "light") == b""
    assert len(cleanup.apply(b"\x01", "light")) == 1
    odd = to_bytes(tone(500)) + b"\x07"
    out = cleanup.apply(odd, "light")
    assert len(out) == len(odd) and out[-1:] == b"\x07"  # the stray byte is passed through
    clipping = array("h", [32767, -32768] * 500)
    out = from_bytes(cleanup.apply(to_bytes(clipping), "boost"))
    assert max(out) <= 32767 and min(out) >= -32768


def test_thirty_seconds_is_fast_enough_for_the_live_path():
    pcm = to_bytes(tone(300, seconds=30, amp=2000))
    t0 = time.monotonic()
    cleanup.apply(pcm, "light")
    assert time.monotonic() - t0 < 3.0  # measured, not assumed; pure Python


def test_nonsense_rate_or_cutoff_leaves_audio_alone_and_never_raises():
    s = tone(500)
    assert cleanup.highpass(s, 0, 100.0) is s
    assert cleanup.highpass(s, RATE, RATE / 2) is s  # cutoff at Nyquist
    assert cleanup.highpass(s, RATE, -5.0) is s
    pcm = to_bytes(s)
    assert cleanup.apply(pcm, "light", rate=0) == pcm  # falls back, no ZeroDivisionError


def test_a_clip_that_starts_loud_has_no_start_up_step():
    dc = array("h", [20000] * 4000)
    out = cleanup.highpass(dc, RATE, 150.0)
    assert max(abs(v) for v in out[:50]) < 200  # was about 19000 with a zeroed filter


def test_filter_gain_at_the_cutoff_is_minus_3_db():
    for cutoff in (80.0, 150.0):
        s = tone(cutoff, seconds=1.0)
        out = cleanup.highpass(s, RATE, cutoff)
        assert abs(rms(out, skip=4000) / rms(s, skip=4000) - 0.7071) < 0.02
