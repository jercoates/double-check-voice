"""Closed-by-default start-up, address validation, capped upstream replies, safe purging."""

import asyncio
import os
import threading
import time
import wave
from datetime import datetime
from functools import partial

import pytest
from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.asr import Transcribe
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler

from dcv_stt_chain import audio_store
from dcv_stt_chain.config import Config
from dcv_stt_chain.handler import Shared, SttChainHandler
from test_passthrough import FakeWhisper, make_cfg, run_command, start

ATTR = Attribution(name="t", url="http://t")


# ---- start-up ---------------------------------------------------------------


def test_config_is_closed_by_default(monkeypatch):
    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    monkeypatch.delenv("DCV_ALLOW_ANY_PEER")
    monkeypatch.delenv("DCV_ALLOWED_PEERS", raising=False)
    with pytest.raises(ValueError, match="DCV_ALLOWED_PEERS"):
        Config.from_env()
    for off in ("0", "", "no", "false"):
        monkeypatch.setenv("DCV_ALLOW_ANY_PEER", off)
        with pytest.raises(ValueError):
            Config.from_env()
    monkeypatch.setenv("DCV_ALLOW_ANY_PEER", "1")
    assert Config.from_env().allow_any_peer is True
    monkeypatch.delenv("DCV_ALLOW_ANY_PEER")
    monkeypatch.setenv("DCV_ALLOWED_PEERS", "192.168.1.5")
    cfg = Config.from_env()
    assert cfg.allowed_peers == frozenset({"192.168.1.5"}) and cfg.allow_any_peer is False


def test_config_rejects_addresses_that_would_fail_on_every_run(monkeypatch):
    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    bad = (
        "192.168.1.10:10300",  # no scheme
        "tcp://x",  # no port
        "tcp://x:0",
        "tcp://x:99999",
        "tcp://x:abc",
        "tcp://:1",
        "unix:///tmp/s",
        "http://x:1",
    )
    for name, original in (
        ("DCV_WHISPER_URI", "tcp://x:1"),
        ("DCV_LISTEN_URI", None),
        ("DCV_PHRASE_URI", None),
    ):
        for value in bad:
            monkeypatch.setenv(name, value)
            with pytest.raises(ValueError, match=name):
                Config.from_env()
        if original is None:
            monkeypatch.delenv(name)
        else:
            monkeypatch.setenv(name, original)
    assert Config.from_env().whisper_uri == "tcp://x:1"


# ---- replies from the engines behind us are size-capped -----------------------


class HugeReply(AsyncEventHandler):
    """Claims a reply far larger than any transcript, then keeps the socket open."""

    header = b'{"type": "transcript", "data_length": 999999999}\n'

    async def handle_event(self, event):
        if AudioStop.is_type(event.type):
            self.writer.write(self.header)
            await self.writer.drain()
            await asyncio.sleep(10)
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "header",
    [
        b'{"type": "transcript", "data_length": 999999999}\n',
        b'{"type": "transcript", "payload_length": 999999999}\n',
        b'{"type": "transcript", "data_length": true}\n',
    ],
)
async def test_oversized_reply_from_whisper_is_refused_not_buffered(tmp_path, capsys, header):
    cls = type("H", (HugeReply,), {"header": header})
    whisper, wport = await start(lambda r, w: cls(r, w))
    cfg = make_cfg(tmp_path, wport, result_timeout=5.0)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        t0 = time.monotonic()
        result = await run_command(pport)
        took = time.monotonic() - t0
    finally:
        await proxy.stop()
        await whisper.stop()
    out = capsys.readouterr().out
    assert result.text == ""
    assert took < 2.0  # refused at the header; not waiting out the 5 s result timeout
    assert '"error": "read: ProtocolError"' in out


class BigInfo(FakeWhisper):
    """Describe reply well over the 64 KB a transcript may use, like a real engine's model list."""

    async def handle_event(self, event):
        if Describe.is_type(event.type):
            models = [
                AsrModel(
                    name=f"model-{i}",
                    description="x" * 100,
                    attribution=ATTR,
                    installed=True,
                    languages=["en"],
                    version="1",
                )
                for i in range(700)
            ]
            info = Info(
                asr=[
                    AsrProgram(
                        name="big", description="big", attribution=ATTR,
                        installed=True, version="1", models=models,
                    )
                ]
            )
            await self.write_event(info.event())
            return True
        return await super().handle_event(event)


@pytest.mark.asyncio
async def test_a_large_describe_reply_still_passes_through(tmp_path):
    whisper, wport = await start(lambda r, w: BigInfo(r, w))
    cfg = make_cfg(tmp_path, wport, connect_timeout=3.0)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(Describe().event())
        event = await asyncio.wait_for(client.read_event(), 5)
        await client.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()
    info = Info.from_event(event)
    assert len(info.asr[0].models) == 700
    assert info.asr[0].name == "dcv-stt-chain-test-dev"


# ---- audio chunks of odd length ----------------------------------------------


class Recorder(FakeWhisper):
    sizes: list = []

    async def handle_event(self, event):
        if AudioChunk.is_type(event.type):
            Recorder.sizes.append(len(event.payload or b""))
        return await super().handle_event(event)


async def _send_chunks(port, sizes):
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{port}")
    await client.connect()
    try:
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        for n in sizes:
            await client.write_event(
                AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01" * n).event()
            )
        await client.write_event(AudioStop().event())
        event = await asyncio.wait_for(client.read_event(), 5)
        await asyncio.sleep(0.4)
        return Transcript.from_event(event)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("sizes,heard", [([3, 5, 1, 7], 16), ([3, 4], 6), ([1], 0)])
async def test_odd_length_chunks_never_shift_the_samples(tmp_path, sizes, heard):
    Recorder.sizes = []
    whisper, wport = await start(lambda r, w: Recorder(r, w))
    cfg = make_cfg(tmp_path, wport)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        result = await _send_chunks(pport, sizes)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == f"heard {heard}"
    assert all(s % 2 == 0 and s > 0 for s in Recorder.sizes)  # whole samples, never empty
    wavs = list((tmp_path / "audio").glob("*/*.wav"))
    if heard:
        with wave.open(str(wavs[0])) as w:
            assert w.getnframes() * 2 == heard


# ---- the audio store only ever touches its own files ---------------------------


def test_purge_and_size_only_touch_this_proxys_own_files(tmp_path):
    old = time.time() - 90 * 86400
    day = tmp_path / "2020-01-01"
    day.mkdir()
    own = day / "010101_t_00000000.wav"
    own.write_bytes(b"x" * 5000)
    own_log = day / "runs_t.jsonl"
    own_log.write_text("{}\n")
    foreign = [
        day / "holiday.wav",
        day / "010101_t_0000000.wav",  # run id one character short
        day / "runs_t.txt",
        tmp_path / "photos" / "a.wav",
        tmp_path / "2020-01-01x" / "010101_t_00000000.wav",
    ]
    for f in foreign:
        f.parent.mkdir(exist_ok=True)
        f.write_bytes(b"x" * 5000)
    for f in [own, own_log, *foreign]:
        os.utime(f, (old, old))
    assert audio_store.dir_size(tmp_path) == 5000  # only the proxy's own clip counts
    removed = audio_store.purge_old(tmp_path, retention_days=30, max_bytes=1)
    assert removed == 1 and not own.exists() and not own_log.exists()
    assert all(f.exists() for f in foreign)


def test_purge_keeps_a_recently_touched_empty_day_folder(tmp_path):
    fresh = tmp_path / "2020-01-01"
    fresh.mkdir()
    audio_store.purge_old(tmp_path, 30)
    assert fresh.exists()  # touched just now: a save may be about to use it
    long_ago = time.time() - 2 * audio_store.FOLDER_IDLE_S
    os.utime(fresh, (long_ago, long_ago))
    audio_store.purge_old(tmp_path, 30)
    assert not fresh.exists()


def test_save_wav_will_not_follow_a_symlinked_day_folder(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    audio = tmp_path / "audio"
    audio.mkdir()
    day = audio / datetime.now().strftime("%Y-%m-%d")
    try:
        day.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this system does not allow symlinks here")
    assert audio_store.save_wav(audio, "t", "abcd1234", b"\x00\x00" * 10, 16000, 2, 1) is None
    assert list(target.iterdir()) == []
    assert audio_store.append_run_log(audio, "t", "{}") is False
    assert list(target.iterdir()) == []


def test_save_wav_never_overwrites_an_existing_file(tmp_path, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 2, 3, 4, 5)

    monkeypatch.setattr(audio_store, "datetime", Frozen)
    first = audio_store.save_wav(tmp_path, "t", "abcd1234", b"\x01\x00" * 10, 16000, 2, 1)
    assert first is not None
    assert audio_store.save_wav(tmp_path, "t", "abcd1234", b"\x02\x00" * 10, 16000, 2, 1) is None
    with wave.open(str(first)) as w:
        assert w.readframes(10) == b"\x01\x00" * 10


@pytest.mark.asyncio
async def test_a_burst_at_the_cap_runs_one_purge_not_one_per_save(tmp_path, monkeypatch):
    real = audio_store.purge_old
    state = {"calls": 0, "running": 0, "max": 0}
    guard = threading.Lock()

    def counting_purge(*args, **kwargs):
        with guard:
            state["calls"] += 1
            state["running"] += 1
            state["max"] = max(state["max"], state["running"])
        time.sleep(0.3)
        try:
            return real(*args, **kwargs)
        finally:
            with guard:
                state["running"] -= 1

    monkeypatch.setattr(audio_store, "purge_old", counting_purge)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, audio_max_mb=1)
    shared = Shared(audio_bytes=1024 * 1024)  # the store is already full
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=shared))
    try:
        results = await asyncio.gather(*(run_command(pport) for _ in range(4)))
    finally:
        await proxy.stop()
        await whisper.stop()
    assert all(r.text == "heard 16000" for r in results)
    assert state["calls"] == 1 and state["max"] == 1


# ---- start-up errors are one clear line and a non-zero exit -------------------


@pytest.mark.parametrize(
    "setup,expect",
    [
        ({"DCV_ALLOW_ANY_PEER": None, "DCV_ALLOWED_PEERS": None}, "DCV_ALLOWED_PEERS is empty"),
        ({"DCV_WHISPER_URI": "192.168.1.10:10300"}, "DCV_WHISPER_URI must look like"),
        ({"DCV_WHISPER_URI": None}, "DCV_WHISPER_URI is not set"),
    ],
)
def test_bad_configuration_exits_with_one_clear_line(monkeypatch, caplog, setup, expect):
    from dcv_stt_chain.__main__ import main

    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    for name, value in setup.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as exit_info:
        asyncio.run(main())
    assert exit_info.value.code == 2
    assert any(expect in r.getMessage() for r in caplog.records)


# ---- second-round fixes -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [b"{" * 70_000 + b"\n", b"[" * 5_000 + b"\n", b"x" * 70_000 + b"\n"],
    ids=["header-over-64KB", "deeply-nested", "long-garbage"],
)
async def test_hostile_header_with_an_open_command_is_answered_not_dropped(tmp_path, capsys, bad):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * 800).event())
        client._writer.write(bad)
        await client._writer.drain()
        event = await asyncio.wait_for(client.read_event(), 5)
        await client.disconnect()
        await asyncio.sleep(0.4)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert Transcript.from_event(event).text == "heard 1600"  # answered with what was heard


class OddText(FakeWhisper):
    """A misbehaving engine whose transcript text is not a string."""

    async def handle_event(self, event):
        if AudioStop.is_type(event.type):
            from wyoming.event import Event

            await self.write_event(Event(type="transcript", data={"text": 5}))
            return True
        return await super().handle_event(event)


@pytest.mark.asyncio
async def test_a_non_string_transcript_from_whisper_does_not_lose_the_log_line(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: OddText(r, w))
    cfg = make_cfg(tmp_path, wport)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == ""
    assert '"whisper_text": ""' in capsys.readouterr().out


def test_numeric_setting_errors_name_the_setting(monkeypatch):
    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    for name in ("DCV_MAX_PER_IP", "DCV_AUDIO_MAX_MB", "DCV_RESULT_TIMEOUT"):
        monkeypatch.setenv(name, "abc")
        with pytest.raises(ValueError, match=name):
            Config.from_env()
        monkeypatch.delenv(name)


@pytest.mark.asyncio
async def test_a_clip_nearly_as_big_as_the_cap_still_makes_room_and_is_saved(tmp_path):
    # Purge target would be 0 ("no limit") if it were not floored: 30 s of audio vs a 1 MiB cap.
    day = tmp_path / "audio" / "2026-01-01"
    day.mkdir(parents=True)
    junk = day / "010101_junk_aaaaaaaa.wav"
    junk.write_bytes(b"x" * 600_000)
    os.utime(junk, (time.time() - 1000,) * 2)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, audio_max_mb=1)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared(audio_bytes=600_000)))
    try:
        result = await run_command(pport, chunks=30, size=32000)  # exactly 30 s
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 960000"
    names = [p.name for p in (tmp_path / "audio").glob("*/*.wav")]
    assert not junk.exists()
    assert any("test-dev" in n for n in names)  # the new clip was saved


@pytest.mark.asyncio
async def test_a_non_string_transcript_from_the_second_engine_keeps_the_log_line(tmp_path, capsys):
    from wyoming.event import Event
    from test_robustness import _compare_run, _last_run, _phrase_cls

    class Odd(_phrase_cls()):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                await self.write_event(Event(type="transcript", data={"text": 5}))
                return True
            return await super().handle_event(event)

    result, _ = await _compare_run(tmp_path, Odd, settle=0.5)
    rec = _last_run(capsys)
    assert result.text == "heard 16000"
    assert rec["phrase_text"] == "" and rec["phrase_matched"] is False and rec["agree"] is False
