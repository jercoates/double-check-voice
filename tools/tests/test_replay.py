"""Replay tool tests: real TCP sockets against small fake engines."""

import argparse
import asyncio
import json
import socket
import sys
import wave
from pathlib import Path

import pytest
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStop
from wyoming.server import AsyncEventHandler, AsyncServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import replay  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Engine(AsyncEventHandler):
    """Answers with a template: {n} = bytes heard, {peak} = loudest sample heard."""

    text = "hello"
    mode = "ok"  # ok | hang | drop

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.nbytes = 0
        self.peak = 0

    async def handle_event(self, event):
        if AudioChunk.is_type(event.type):
            data = event.payload or b""
            self.nbytes += len(data)
            for i in range(0, len(data) - 1, 2):
                self.peak = max(self.peak, abs(int.from_bytes(data[i : i + 2], "little", signed=True)))
        elif AudioStop.is_type(event.type):
            if self.mode == "drop":
                return False
            if self.mode == "hang":
                await asyncio.sleep(30)
            await self.write_event(
                Transcript(text=type(self).text.format(n=self.nbytes, peak=self.peak)).event()
            )
        return True


async def start(text="hello", mode="ok"):
    cls = type("E", (Engine,), {"text": text, "mode": mode})
    port = free_port()
    server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
    await server.start(lambda r, w: cls(r, w))
    return server, f"tcp://127.0.0.1:{port}"


def make_wav(path: Path, frames=16000, rate=16000, amp=1000):
    """A 440 Hz tone (a constant level would be removed by the high-pass, as it should be)."""
    import math

    data = b"".join(
        int(amp * math.sin(2 * math.pi * 440 * i / 16000)).to_bytes(2, "little", signed=True)
        for i in range(frames)
    )
    with wave.open(str(path), "wb") as w:
        w.setframerate(rate)
        w.setsampwidth(2)
        w.setnchannels(1)
        w.writeframes(data)


def ns(**kw):
    base = dict(clips=[], engine=None, whisper="", phrase="", language="en", timeout=5.0, expected=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_wer_and_norm():
    assert replay.norm("Turn ON, the light!") == "turn on the light"
    assert replay.wer("turn on the light", "turn on the light") == 0.0
    assert replay.wer("turn on the light", "turn off the light") == 0.25
    assert replay.wer("turn on", "") == 1.0
    assert replay.wer("", "") == 0.0 and replay.wer("", "x") == 1.0


def test_engine_specs():
    assert replay.parse_engine("w=tcp://h:1") == ("w", "tcp://h:1", "none")
    assert replay.parse_engine("w2=tcp://h:1@light") == ("w2", "tcp://h:1", "light")
    assert replay.parse_engine("w=tcp://[::1]:10300@light") == ("w", "tcp://[::1]:10300", "light")
    assert replay.parse_engine("w=tcp://u@h:1@boost") == ("w", "tcp://u@h:1", "boost")
    for bad in ("nourl", "=tcp://h:1", "w=h:1", "w=tcp://h:1@heavy", "a b=tcp://h:1"):
        with pytest.raises(ValueError):
            replay.parse_engine(bad)


@pytest.mark.asyncio
async def test_clip_goes_to_both_engines_and_is_scored(tmp_path):
    make_wav(tmp_path / "a.wav", frames=16000)
    (tmp_path / "expected.csv").write_text("a.wav,heard 32000\n")
    ws, wuri = await start("heard {n}")
    ps, puri = await start("turn on the light")
    try:
        rows = await replay.run(
            ns(clips=[str(tmp_path)], whisper=wuri, phrase=puri, expected=str(tmp_path / "expected.csv"))
        )
    finally:
        await ws.stop()
        await ps.stop()
    r = rows[0]
    assert r["engines"]["whisper"]["text"] == "heard 32000"  # the whole clip arrived, in chunks
    assert r["engines"]["phrase"]["text"] == "turn on the light"
    assert r["agree_with_first"] == {"phrase": False}
    assert r["whisper_exact"] is True and r["phrase_exact"] is False
    s = replay.summarize(rows)
    assert s["clips"] == 1 and s["whisper"]["exact"] == "1/1" and s["phrase"]["nonempty"] == 1


@pytest.mark.asyncio
async def test_three_engines_and_cleanup_applies_only_to_the_engine_that_asks(tmp_path):
    make_wav(tmp_path / "a.wav", frames=8000, amp=500)  # a quiet clip
    srv = [await start("peak {peak}") for _ in range(3)]
    try:
        specs = [f"raw={srv[0][1]}", f"cleaned={srv[1][1]}@boost", f"raw2={srv[2][1]}"]
        rows = await replay.run(ns(clips=[str(tmp_path)], engine=specs))
    finally:
        for s, _ in srv:
            await s.stop()
    e = rows[0]["engines"]
    assert e["raw"]["text"] == e["raw2"]["text"]
    assert 480 <= int(e["raw"]["text"].split()[1]) <= 500  # the clip as recorded
    assert int(e["cleaned"]["text"].split()[1]) > 5000  # boosted toward -3 dBFS
    assert rows[0]["agree_with_first"] == {"cleaned": False, "raw2": True}
    summary = replay.summarize(rows)
    assert set(summary) == {"clips", "raw", "cleaned", "raw2"}
    assert summary["raw2"]["agree_with_first"] == 1


def test_engine_list_rules():
    with pytest.raises(ValueError):
        replay.engines_from_args(ns())  # none
    with pytest.raises(ValueError):
        replay.engines_from_args(ns(engine=["a=tcp://h:1", "a=tcp://h:2"]))  # duplicate name
    with pytest.raises(ValueError):
        replay.engines_from_args(ns(engine=[f"e{i}=tcp://h:{i}" for i in range(replay.MAX_ENGINES + 1)]))


@pytest.mark.asyncio
async def test_dead_hung_and_dropping_engines_are_reported_not_fatal(tmp_path):
    hang, huri = await start(mode="hang")
    drop, duri = await start(mode="drop")
    dead = f"tcp://127.0.0.1:{free_port()}"
    try:
        for uri, want in ((huri, "timeout"), (duri, None), (dead, None)):
            out = await replay.ask(uri, b"\x01\x00" * 800, "en", 1.0)
            assert out["text"] == "" and out["error"]
            if want:
                assert out["error"] == want
    finally:
        await hang.stop()
        await drop.stop()


def test_bad_clips_are_skipped(tmp_path, capsys):
    make_wav(tmp_path / "wrong_rate.wav", rate=8000)
    (tmp_path / "junk.wav").write_bytes(b"not a wav")
    assert replay.read_pcm(tmp_path / "wrong_rate.wav") is None
    assert replay.read_pcm(tmp_path / "junk.wav") is None
    assert "skipping" in capsys.readouterr().err
    assert replay.find_clips([str(tmp_path / "nope")]) == []


@pytest.mark.asyncio
async def test_main_writes_results_file_with_the_settings_used(tmp_path):
    make_wav(tmp_path / "a.wav")
    ws, wuri = await start("ok")
    out = tmp_path / "r.jsonl"
    try:
        rc = await asyncio.get_running_loop().run_in_executor(
            None, replay.main,
            [str(tmp_path / "a.wav"), "--engine", f"w={wuri}@light", "--out", str(out)],
        )
    finally:
        await ws.stop()
    lines = [json.loads(l) for l in out.read_text().splitlines()]
    assert rc == 0 and lines[0]["summary"]["clips"] == 1 and lines[1]["clip"] == "a.wav"
    assert lines[0]["settings"]["engines"]["w"]["cleanup"] == "light"


@pytest.mark.asyncio
async def test_an_engine_that_stops_reading_does_not_hang_the_replay():
    # Accepts the connection, never reads: the sender's buffers fill and the writes stall.
    async def silent(reader, writer):
        await asyncio.sleep(30)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        big = b"\x01\x00" * 16000 * 30  # 30 s of audio, about 960 KB
        out = await asyncio.wait_for(replay.ask(f"tcp://127.0.0.1:{port}", big, "en", 1.0), 8)
    finally:
        server.close()
    assert out["text"] == "" and out["error"] == "timeout"


def test_expected_file_with_a_bom_still_matches(tmp_path):
    f = tmp_path / "e.csv"
    f.write_bytes(b"\xef\xbb\xbfa.wav,turn on the light\n")
    assert replay.load_expected(str(f)) == {"a.wav": "turn on the light"}
