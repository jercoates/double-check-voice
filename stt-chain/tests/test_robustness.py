"""Failure-path tests: stalled upstream, aborted clients, hostile input, disk limits."""

import asyncio
import json
import os
import time
import wave
from functools import partial

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.info import Describe

from dcv_stt_chain import audio_store
from dcv_stt_chain.handler import Shared, SttChainHandler
from test_passthrough import FakeWhisper, free_port, make_cfg, run_command, start


@pytest.mark.asyncio
async def test_slow_whisper_answers_within_deadline_and_logs_timeout(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w, delay=3.0))
    cfg = make_cfg(tmp_path, wport, result_timeout=0.4)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    t0 = asyncio.get_running_loop().time()
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == ""
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert '"error": "timeout"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_whisper_that_stops_reading_does_not_hang_the_proxy(tmp_path):
    async def accept_and_ignore(reader, writer):
        await asyncio.sleep(30)  # never reads, never answers

    port = free_port()
    stuck = await asyncio.start_server(accept_and_ignore, "127.0.0.1", port)
    cfg = make_cfg(tmp_path, port, connect_timeout=0.5, result_timeout=1.0, max_capture_s=120.0)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    t0 = asyncio.get_running_loop().time()
    try:
        result = await run_command(pport, chunks=100, size=32000)  # 3.2 MB: fills buffers
    finally:
        await proxy.stop()
        stuck.close()
    assert result.text == ""
    assert asyncio.get_running_loop().time() - t0 < 6.0
    assert len(list((tmp_path / "audio").glob("*/*.wav"))) == 1


@pytest.mark.asyncio
async def test_client_disconnect_mid_stream_keeps_audio_and_logs(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * 1600).event()
        )
        await asyncio.sleep(0.2)
        await client.disconnect()
        await asyncio.sleep(0.5)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert len(list((tmp_path / "audio").glob("*/*.wav"))) == 1
    assert '"error": "client_disconnected"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_no_transcribe_event_and_two_commands_on_one_connection(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        texts = []
        for n in (2, 4):
            await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
            for _ in range(n):
                await client.write_event(
                    AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * 1600).event()
                )
            await client.write_event(AudioStop().event())
            event = await asyncio.wait_for(client.read_event(), 5)
            texts.append(Transcript.from_event(event).text)
        await client.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()
    assert texts == ["heard 6400", "heard 12800"]


@pytest.mark.asyncio
async def test_huge_declared_payload_is_refused_without_reading_it(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        writer.write(b'{"type":"audio-chunk","payload_length":50000000000}\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 5) == b""  # closed by the proxy
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_garbage_header_closes_connection(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    junk_list = (b"[1,2]\n", b'{"type":5}\n', b'{"type":"x","data_length":"9"}\n', b"not json\n")
    try:
        for junk in junk_list:
            reader, writer = await asyncio.open_connection("127.0.0.1", pport)
            writer.write(junk)
            await writer.drain()
            assert await asyncio.wait_for(reader.read(), 5) == b""
            writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_idle_client_is_dropped(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, idle_timeout=0.3)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        assert await asyncio.wait_for(reader.read(), 3) == b""  # sent nothing; proxy hung up
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_connection_limit(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport)
    shared = Shared(max_connections=1)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=shared))
    try:
        _, w1 = await asyncio.open_connection("127.0.0.1", pport)  # holds the one slot
        await asyncio.sleep(0.2)
        r2, w2 = await asyncio.open_connection("127.0.0.1", pport)
        assert await asyncio.wait_for(r2.read(), 3) == b""  # refused
        w1.close()
        w2.close()
    finally:
        await proxy.stop()
        await whisper.stop()


def test_purge_removes_old_files_and_enforces_size_cap(tmp_path):
    day = tmp_path / "2026-01-01"
    day.mkdir()
    old = day / "000000_t_00000000.wav"
    old.write_bytes(b"x" * 5000)
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    for i in range(3):
        f = day / f"00001{i}_t_0000001{i}.wav"
        f.write_bytes(b"x" * 5000)
        os.utime(f, (time.time() - (3 - i) * 60,) * 2)
    removed = audio_store.purge_old(tmp_path, retention_days=30, max_bytes=11000)
    assert removed == 2  # the 40-day-old one, then the oldest new one for the size cap
    assert sorted(p.name for p in day.glob("*.wav")) == [
        "000011_t_00000011.wav",
        "000012_t_00000012.wav",
    ]


def test_purge_survives_missing_dir(tmp_path):
    assert audio_store.purge_old(tmp_path / "nope", 30, 100) == 0


@pytest.mark.asyncio
async def test_audio_cap_evicts_oldest_so_new_audio_is_always_kept(tmp_path):
    day = tmp_path / "audio" / "2026-01-01"
    day.mkdir(parents=True)
    for i, name in enumerate(("010101_junk_aaaaaaaa.wav", "010102_junk_bbbbbbbb.wav")):
        f = day / name
        f.write_bytes(b"x" * 600_000)
        os.utime(f, (time.time() - 1000 + i,) * 2)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, audio_max_mb=1)  # 1 MiB cap, 1.2 MB already stored
    shared = Shared(audio_bytes=1_200_000)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=shared))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"
    names = sorted(p.name for p in (tmp_path / "audio").glob("*/*.wav"))
    assert "010101_junk_aaaaaaaa.wav" not in names  # oldest evicted
    assert any("test-dev" in n for n in names)  # the new command's audio was kept


@pytest.mark.asyncio
async def test_stop_without_start_and_chunk_without_start_get_empty_answers(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        c = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await c.connect()
        await c.write_event(Transcribe(language="en").event())
        await c.write_event(AudioStop().event())
        assert Transcript.from_event(await asyncio.wait_for(c.read_event(), 5)).text == ""
        await c.disconnect()

        c = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await c.connect()
        await c.write_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 100).event()
        )
        assert Transcript.from_event(await asyncio.wait_for(c.read_event(), 5)).text == ""
        assert await asyncio.wait_for(c.read_event(), 5) is None
        await c.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_command_that_never_ends_is_answered_not_dropped(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, max_capture_s=0.2, idle_timeout=0.2)  # limit 0.4 s
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        c = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await c.connect()
        await c.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        reader_task = asyncio.ensure_future(c.read_event())
        for _ in range(20):
            try:
                await c.write_event(
                    AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 160).event()
                )
            except ConnectionError:
                break  # the proxy already answered and hung up
            await asyncio.sleep(0.05)
        answer = await asyncio.wait_for(reader_task, 5)
        assert answer is not None and Transcript.is_type(answer.type)
        try:
            await c.disconnect()
        except ConnectionError:
            pass  # server already hung up; Windows reports the reset on close
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_connection_lifetime_is_capped_even_if_client_keeps_chatting(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, max_connection_s=0.6, idle_timeout=5.0)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        closed = False
        for _ in range(40):
            writer.write(b'{"type":"noise"}\n')
            try:
                await writer.drain()
            except ConnectionError:
                closed = True
                break
            await asyncio.sleep(0.05)
            if reader.at_eof():
                closed = True
                break
        assert closed
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_per_ip_limit(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    shared = Shared(max_connections=16, max_per_ip=2)
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport), shared=shared))
    try:
        keep = [await asyncio.open_connection("127.0.0.1", pport) for _ in range(2)]
        await asyncio.sleep(0.2)
        r3, w3 = await asyncio.open_connection("127.0.0.1", pport)
        assert await asyncio.wait_for(r3.read(), 3) == b""  # third from the same IP: refused
        for _, w in keep:
            w.close()
        w3.close()
        await asyncio.sleep(0.3)
        assert shared.active == 0 and not shared.per_ip  # counters released
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_peer_allowlist_refuses_other_hosts(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, allowed_peers=frozenset({"10.9.9.9"}))
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        # Another host (here 127.0.0.2) that is not on the list is refused...
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", pport, local_addr=("127.0.0.2", 0)
        )
        assert await asyncio.wait_for(reader.read(), 3) == b""
        writer.close()
        # ...but loopback always works, because it is the container's own health check.
        c = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await c.connect()
        await c.write_event(Describe().event())
        assert await asyncio.wait_for(c.read_event(), 5) is not None
        await c.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()


def test_health_check_targets_the_bound_address():
    from dcv_stt_chain.healthcheck import health_uri

    assert health_uri("tcp://0.0.0.0:10310") == "tcp://127.0.0.1:10310"
    assert health_uri("tcp://:10310") == "tcp://127.0.0.1:10310"
    assert health_uri("tcp://192.168.1.20:10310") == "tcp://192.168.1.20:10310"
    assert health_uri("tcp://unraid.lan:10310") == "tcp://unraid.lan:10310"
    # IPv6 literals keep their brackets, and :: is checked through ::1
    assert health_uri("tcp://[fe80::1]:10310") == "tcp://[fe80::1]:10310"
    assert health_uri("tcp://[::1]:10310") == "tcp://[::1]:10310"
    assert health_uri("tcp://[::]:10310") == "tcp://[::1]:10310"


def test_health_uri_works_with_the_real_client_parser():
    # The URI must be understood by the same client class the health check uses.
    from wyoming.client import AsyncClient

    from dcv_stt_chain.healthcheck import health_uri

    expected = {
        "tcp://[fe80::1]:10310": "fe80::1",
        "tcp://[::]:10310": "::1",
        "tcp://0.0.0.0:10310": "127.0.0.1",
    }
    for listen, host in expected.items():
        client = AsyncClient.from_uri(health_uri(listen))
        assert client.host == host and client.port == 10310


@pytest.mark.asyncio
async def test_own_bound_address_is_allowed_with_an_allowlist(tmp_path):
    # Host networking, bound to one IP: the health check arrives from that IP itself.
    # The configured name is a host name, not an IP, so the match must use the socket.
    from wyoming.server import AsyncServer

    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(
        tmp_path,
        wport,
        listen_uri="tcp://unraid.lan:10310",
        allowed_peers=frozenset({"10.9.9.9"}),
    )
    pport = free_port()
    proxy = AsyncServer.from_uri(f"tcp://127.0.0.2:{pport}")
    await proxy.start(partial(SttChainHandler, cfg))
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.2", pport, local_addr=("127.0.0.2", 0)
        )
        from wyoming.event import async_read_event, async_write_event

        await async_write_event(Describe().event(), writer)
        assert await asyncio.wait_for(async_read_event(reader), 5) is not None
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_protocol_error_mid_command_still_answers(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", pport)
        from wyoming.event import async_write_event

        await async_write_event(AudioStart(rate=16000, width=2, channels=1).event(), writer)
        await async_write_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 160).event(), writer
        )
        writer.write(b"garbage\n")
        await writer.drain()
        from wyoming.event import async_read_event

        answer = await asyncio.wait_for(async_read_event(reader), 5)
        assert answer is not None and Transcript.from_event(answer).text == "heard 320"
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_allowed_peer_connects(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, allowed_peers=frozenset({"127.0.0.2"}))
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", pport, local_addr=("127.0.0.2", 0)
        )
        from wyoming.event import async_read_event, async_write_event

        await async_write_event(Describe().event(), writer)
        assert await asyncio.wait_for(async_read_event(reader), 5) is not None
        writer.close()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_slow_disk_write_that_outlives_the_wait_still_saves_the_right_clip(
    tmp_path, monkeypatch, capsys
):
    import dcv_stt_chain.handler as handler_mod

    real_purge = audio_store.purge_old

    def slow_purge(*args, **kwargs):
        time.sleep(0.8)  # a stalled disk, in the stage BEFORE the file is written
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(handler_mod, "_SAVE_WAIT_S", 0.2)
    monkeypatch.setattr(audio_store, "purge_old", slow_purge)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, audio_max_mb=1)
    shared = Shared(audio_bytes=1024 * 1024)  # cap reached: the save purges first
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=shared))
    try:
        result = await run_command(pport)  # 16000 bytes = 8000 frames
        await asyncio.sleep(1.5)  # the abandoned save finishes after _reset()
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"  # HA was not held up
    out = capsys.readouterr().out
    assert '"audio_error": "audio_save_slow"' in out
    run_id = json.loads(out.strip().splitlines()[0])["run"]
    wavs = list((tmp_path / "audio").glob("*/*.wav"))
    assert len(wavs) == 1 and run_id in wavs[0].name  # named for the right run
    with wave.open(str(wavs[0])) as w:
        assert w.getnframes() == 8000  # the real command, not an empty clip


def test_config_peer_allowlist_is_validated_and_normalised(monkeypatch):
    from dcv_stt_chain.config import Config

    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    monkeypatch.setenv("DCV_ALLOWED_PEERS", " 192.168.1.5 , ::ffff:192.168.1.6,FE80::1 ")
    assert Config.from_env().allowed_peers == frozenset({"192.168.1.5", "192.168.1.6", "fe80::1"})
    for bad in ("192.168.1.0/24", "192.168.1.5:8123", "homeassistant.local"):
        monkeypatch.setenv("DCV_ALLOWED_PEERS", bad)
        with pytest.raises(ValueError):
            Config.from_env()
    monkeypatch.delenv("DCV_ALLOWED_PEERS")
    monkeypatch.setenv("DCV_MAX_CAPTURE_S", "1e308")
    with pytest.raises(ValueError):
        Config.from_env()


def test_config_rejects_bad_values(monkeypatch):
    from dcv_stt_chain.config import Config

    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    assert Config.from_env().allowed_peers == frozenset()
    for name, bad in (
        ("DCV_RESULT_TIMEOUT", "nan"),
        ("DCV_IDLE_TIMEOUT", "inf"),
        ("DCV_IDLE_TIMEOUT", "0"),
        ("DCV_MAX_CONNECTIONS", "-1"),
        ("DCV_MAX_PER_IP", "-1"),
        ("DCV_AUDIO_MAX_MB", "-5"),
        ("DCV_DEVICE", "../evil"),
    ):
        monkeypatch.setenv(name, bad)
        with pytest.raises(ValueError):
            Config.from_env()
        monkeypatch.delenv(name)


@pytest.mark.asyncio
async def test_client_that_goes_quiet_mid_command_still_gets_an_answer(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, idle_timeout=0.4)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        c = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await c.connect()
        await c.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await c.write_event(
            AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00" * 160).event()
        )
        answer = await asyncio.wait_for(c.read_event(), 5)  # then silence: idle timeout
        assert answer is not None and Transcript.from_event(answer).text == "heard 320"
        await c.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_whisper_error_event_ends_the_wait_early(tmp_path, capsys):
    from wyoming.error import Error

    class Erroring(FakeWhisper):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                await self.write_event(Error(text="boom").event())
                await asyncio.sleep(5)
                return True
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: Erroring(r, w))
    cfg = make_cfg(tmp_path, wport, result_timeout=4.0)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    t0 = asyncio.get_running_loop().time()
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == ""
    assert asyncio.get_running_loop().time() - t0 < 2.0
    assert '"error": "upstream error"' in capsys.readouterr().out


def test_purge_does_not_overdelete_when_a_file_vanished(tmp_path, monkeypatch):
    day = tmp_path / "2026-01-01"
    day.mkdir()
    for i in range(3):
        f = day / f"00000{i}_t_0000000{i}.wav"
        f.write_bytes(b"x" * 5000)
        os.utime(f, (time.time() - (10 - i),) * 2)
    real_unlink = type(day).unlink

    def flaky_unlink(self, *a, **k):
        if self.name == "000000_t_00000000.wav":  # a concurrent purge already removed it
            real_unlink(self)
            raise FileNotFoundError(self.name)
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(type(day), "unlink", flaky_unlink)
    audio_store.purge_old(tmp_path, retention_days=0, max_bytes=11000)
    assert sorted(p.name for p in day.glob("*.wav")) == [
        "000001_t_00000001.wav",
        "000002_t_00000002.wav",
    ]  # only one needed


@pytest.mark.asyncio
async def test_chatty_whisper_cannot_extend_the_deadline(tmp_path):
    from wyoming.event import Event

    class Chatty(FakeWhisper):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                for _ in range(200000):
                    await self.write_event(Event(type="noise"))
                    await asyncio.sleep(0)
                return True
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: Chatty(r, w))
    cfg = make_cfg(tmp_path, wport, result_timeout=0.5)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    t0 = asyncio.get_running_loop().time()
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == ""
    assert asyncio.get_running_loop().time() - t0 < 3.0


@pytest.mark.asyncio
async def test_unwritable_audio_dir_is_reported_in_log(tmp_path, capsys):
    blocker = tmp_path / "audio"
    blocker.write_text("a file where the folder should be")
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"
    assert '"audio_error": "audio_save_failed"' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_log_file_matches_stdout_and_sits_next_to_the_clip(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        await run_command(pport)
        await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    out = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    logs = list((tmp_path / "audio").glob("*/runs_test-dev.jsonl"))
    assert len(logs) == 1
    assert (logs[0].stat().st_mode & 0o077) == 0 or os.name == "nt"
    lines = [json.loads(l) for l in logs[0].read_text(encoding="utf-8").splitlines()]
    assert lines == out and len(lines) == 2
    assert os.path.dirname(lines[0]["audio"]) == str(logs[0].parent)


@pytest.mark.asyncio
async def test_run_log_file_can_be_switched_off(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(
        partial(SttChainHandler, make_cfg(tmp_path, wport, run_log_file=False))
    )
    try:
        await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert list((tmp_path / "audio").glob("*/*.wav"))
    assert not list((tmp_path / "audio").glob("*/*.jsonl"))


@pytest.mark.asyncio
async def test_unwritable_run_log_does_not_break_the_answer(tmp_path, capsys):
    blocker = tmp_path / "audio"
    blocker.write_text("a file where the folder should be")
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"
    assert '"run":' in capsys.readouterr().out  # stdout line still written


def test_purge_removes_old_run_logs_and_keeps_new_ones(tmp_path):
    old = tmp_path / "2020-01-01"
    new = tmp_path / "2999-01-01"
    old.mkdir()
    new.mkdir()
    (old / "runs_a.jsonl").write_text("{}\n")
    (new / "runs_a.jsonl").write_text("{}\n")
    ancient = time.time() - 40 * 86400
    os.utime(old / "runs_a.jsonl", (ancient, ancient))
    audio_store.purge_old(tmp_path, 30)
    assert not (old / "runs_a.jsonl").exists()
    assert (new / "runs_a.jsonl").exists()
    os.utime(old, (ancient, ancient))  # emptied just now: kept until it has been idle
    audio_store.purge_old(tmp_path, 30)
    assert not old.exists()


@pytest.mark.asyncio
async def test_failing_run_log_with_a_working_audio_dir_still_answers_and_next_run_works(
    tmp_path, capsys, monkeypatch
):
    def boom(*a, **k):
        raise OSError("disk gone")

    monkeypatch.setattr(audio_store.os, "write", boom)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        first = await run_command(pport)
        monkeypatch.undo()
        second = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert first.text == second.text == "heard 16000"
    assert len(list((tmp_path / "audio").glob("*/*.wav"))) == 2  # audio unaffected
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert len(out) == 2  # stdout line written even though the file write failed
    logs = list((tmp_path / "audio").glob("*/runs_test-dev.jsonl"))
    assert len(logs[0].read_text(encoding="utf-8").splitlines()) == 1  # only the 2nd run


@pytest.mark.asyncio
async def test_run_log_is_skipped_when_the_audio_write_already_stalled(tmp_path, monkeypatch):
    from dcv_stt_chain import handler as handler_mod

    monkeypatch.setattr(handler_mod, "_SAVE_WAIT_S", 0.2)
    real = audio_store.save_wav

    def slow(*a, **k):
        time.sleep(0.6)
        return real(*a, **k)

    monkeypatch.setattr(audio_store, "save_wav", slow)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        t0 = time.monotonic()
        await run_command(pport)
        assert time.monotonic() - t0 < 2.0
    finally:
        await proxy.stop()
        await whisper.stop()
    await asyncio.sleep(0.8)
    assert not list((tmp_path / "audio").glob("*/*.jsonl"))


def test_run_log_line_is_one_write_and_devices_get_separate_files(tmp_path):
    assert audio_store.append_run_log(tmp_path, "a", '{"x": 1}')
    assert audio_store.append_run_log(tmp_path, "a", '{"x": 2}')
    assert audio_store.append_run_log(tmp_path, "b", '{"x": 3}')
    day = next(tmp_path.iterdir())
    assert (day / "runs_a.jsonl").read_text() == '{"x": 1}\n{"x": 2}\n'
    assert (day / "runs_b.jsonl").read_text() == '{"x": 3}\n'


@pytest.mark.parametrize(
    "value,expected",
    [(None, True), ("1", True), ("0", False), (" False ", False), ("OFF", False), ("no", False), ("yes", True)],
)
def test_run_log_setting_parsing(monkeypatch, value, expected):
    from dcv_stt_chain.config import Config

    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    if value is None:
        monkeypatch.delenv("DCV_RUN_LOG_FILE", raising=False)
    else:
        monkeypatch.setenv("DCV_RUN_LOG_FILE", value)
    assert Config.from_env().run_log_file is expected


async def _raw_command(port, language, start_stop_only=False, raw_header=None):
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{port}")
    await client.connect()
    try:
        if raw_header is not None:
            client._writer.write(raw_header)  # bytes a normal client could not build
            await client._writer.drain()
        else:
            await client.write_event(Transcribe(language=language).event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        if not start_stop_only:
            await client.write_event(
                AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * 800).event()
            )
        await client.write_event(AudioStop().event())
        await asyncio.wait_for(client.read_event(), 5)
        await asyncio.sleep(0.3)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_hostile_language_is_dropped_and_the_run_is_still_logged(tmp_path, capsys):
    seen = []

    class Spy(FakeWhisper):
        async def handle_event(self, event):
            seen.append(event)
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: Spy(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        await _raw_command(
            pport, None,
            raw_header=b'{"type": "transcribe", "data": {"language": "\\ud800\\u2028x"}}\n',
        )
    finally:
        await proxy.stop()
        await whisper.stop()
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    assert len(out) == 1 and json.loads(out[0])["language"] is None
    assert out[0].isascii()
    tr = [e for e in seen if e.type == "transcribe"]
    assert tr and tr[0].data.get("language") is None


@pytest.mark.asyncio
async def test_zero_audio_runs_do_not_grow_the_run_log_file(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        for _ in range(3):
            await _raw_command(pport, "en", start_stop_only=True)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert not list((tmp_path / "audio").glob("*/*.jsonl"))


def test_run_log_stops_at_its_size_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_store, "MAX_RUNLOG_BYTES", 30)
    assert audio_store.append_run_log(tmp_path, "a", "x" * 20)
    assert not audio_store.append_run_log(tmp_path, "a", "x" * 20)
    day = next(tmp_path.iterdir())
    assert (day / "runs_a.jsonl").stat().st_size == 21


def test_tiny_clips_count_a_disk_block_toward_the_cap(tmp_path):
    day = tmp_path / "2020-01-01"
    day.mkdir()
    (day / "010101_t_00000000.wav").write_bytes(b"x")
    assert audio_store.dir_size(tmp_path) == audio_store.MIN_FILE_COST


@pytest.mark.asyncio
async def test_only_validated_fields_reach_whisper(tmp_path):
    seen = []

    class Spy(FakeWhisper):
        async def handle_event(self, event):
            seen.append(event)
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: Spy(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
    await client.connect()
    try:
        from wyoming.event import Event

        await client.write_event(
            Event(type="transcribe", data={"language": "en", "name": "x", "context": {"a": 1}})
        )
        await client.write_event(
            Event(type="audio-start", data={"rate": 16000, "width": 2, "channels": 1, "timestamp": 5, "k": 1})
        )
        await client.write_event(
            Event(
                type="audio-chunk",
                data={"rate": 8000, "width": 2, "channels": 1, "timestamp": 9, "k": 1},
                payload=b"\x01\x00" * 800,
            )
        )
        await client.write_event(AudioStop().event())
        await asyncio.wait_for(client.read_event(), 5)
    finally:
        await client.disconnect()
        await proxy.stop()
        await whisper.stop()
    for e in seen:
        if e.type == "transcribe":
            assert e.data == {"language": "en"}
        if e.type == "audio-start":
            assert e.data == {"rate": 16000, "width": 2, "channels": 1, "timestamp": None}
        if e.type == "audio-chunk":
            assert e.data == {"rate": 16000, "width": 2, "channels": 1, "timestamp": None}
    assert {"transcribe", "audio-start", "audio-chunk"} <= {e.type for e in seen}


class FakePhrase(FakeWhisper):
    """Stands in for Speech-to-Phrase: fixed text, or silence ('hang') or a dropped socket."""

    text = "turn on the light"
    hang = False
    drop = False
    heard = 0

    async def handle_event(self, event):
        if AudioChunk.is_type(event.type):
            type(self).heard += len(event.payload or b"")
        elif AudioStop.is_type(event.type):
            if self.drop:
                return False
            if self.hang:
                await asyncio.sleep(30)
                return True
            await self.write_event(Transcript(text=type(self).text).event())
            return True
        return await super().handle_event(event)


def _phrase_cls(**attrs):
    return type("P", (FakePhrase,), {"heard": 0, **attrs})


async def _compare_run(tmp_path, phrase_factory, phrase_port=None, settle=0.0, **over):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    phrase = None
    if phrase_factory is not None:
        phrase, phrase_port = await start(lambda r, w: phrase_factory(r, w))
    cfg = make_cfg(
        tmp_path, wport, mode="compare", phrase_uri=f"tcp://127.0.0.1:{phrase_port}", **over
    )
    proxy, pport = await start(partial(SttChainHandler, cfg))
    t0 = time.monotonic()
    try:
        result = await run_command(pport)
        took = time.monotonic() - t0
        await asyncio.sleep(settle)  # the log line follows the shadow's deadline
    finally:
        await proxy.stop()
        await whisper.stop()
        if phrase:
            await phrase.stop()
    return result, took


def _last_run(capsys):
    out = [l for l in capsys.readouterr().out.splitlines() if l.startswith("{")]
    return json.loads(out[-1])


@pytest.mark.asyncio
async def test_compare_returns_whispers_text_and_logs_both(tmp_path, capsys):
    cls = _phrase_cls()
    result, _ = await _compare_run(tmp_path, cls)
    rec = _last_run(capsys)
    assert result.text == "heard 16000"  # HA still hears Whisper, never the shadow
    assert rec["mode"] == "compare" and rec["whisper_text"] == "heard 16000"
    assert rec["phrase_text"] == "turn on the light" and rec["phrase_matched"] is True
    assert rec["agree"] is False and rec["phrase_error"] is None
    assert rec["latency_ms"]["phrase_after_stop"] is not None
    assert cls.heard == 16000  # the shadow heard the same audio


@pytest.mark.asyncio
async def test_compare_agree_ignores_case_and_punctuation(tmp_path, capsys):
    await _compare_run(tmp_path, _phrase_cls(text="Heard 16000."))
    assert _last_run(capsys)["agree"] is True


@pytest.mark.asyncio
async def test_compare_phrase_no_match_is_logged_as_unmatched(tmp_path, capsys):
    await _compare_run(tmp_path, _phrase_cls(text=""))
    rec = _last_run(capsys)
    assert rec["phrase_matched"] is False and rec["agree"] is False and rec["phrase_error"] is None


@pytest.mark.asyncio
async def test_compare_with_phrase_down_changes_nothing_for_ha(tmp_path, capsys):
    port = free_port()  # nothing listens here
    result, took = await _compare_run(tmp_path, None, phrase_port=port, settle=1.5)
    rec = _last_run(capsys)
    assert result.text == "heard 16000" and took < 2.0
    assert rec["phrase_error"].startswith("connect") and rec["phrase_matched"] is False
    assert rec["error"] is None


# What a dropped shadow socket can look like, depending on the platform.
_DROPPED = ("closed", "read: ConnectionResetError", "read: IncompleteReadError")


@pytest.mark.asyncio
async def test_compare_with_phrase_hanging_does_not_delay_ha(
    tmp_path, capsys
):
    result, took = await _compare_run(
        tmp_path, _phrase_cls(hang=True), phrase_timeout=0.5, settle=1.0
    )
    rec = _last_run(capsys)
    assert result.text == "heard 16000"
    assert rec["phrase_error"] == "timeout" and rec["error"] is None
    assert took < 1.5


@pytest.mark.asyncio
async def test_compare_with_phrase_dropping_the_socket(tmp_path, capsys):
    result, _ = await _compare_run(tmp_path, _phrase_cls(drop=True))
    rec = _last_run(capsys)
    assert result.text == "heard 16000" and rec["phrase_error"] in _DROPPED


def test_compare_needs_a_phrase_uri_and_mode_is_validated(monkeypatch):
    from dcv_stt_chain.config import Config

    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    monkeypatch.setenv("DCV_MODE", "compare")
    monkeypatch.delenv("DCV_PHRASE_URI", raising=False)
    with pytest.raises(ValueError):
        Config.from_env()
    monkeypatch.setenv("DCV_PHRASE_URI", "tcp://y:2")
    assert Config.from_env().mode == "compare"
    monkeypatch.setenv("DCV_MODE", "fallback")
    with pytest.raises(ValueError):
        Config.from_env()


@pytest.mark.asyncio
async def test_compare_phrase_error_values_and_profile(tmp_path, capsys):
    await _compare_run(tmp_path, _phrase_cls(drop=True))
    rec = _last_run(capsys)
    assert rec["phrase_error"] in _DROPPED
    assert rec["profile"]["phrase_uri"].startswith("tcp://127.0.0.1:")
    assert rec["profile"]["phrase_timeout"] == 3.0


@pytest.mark.asyncio
async def test_compare_phrase_error_event_is_logged_not_used(tmp_path, capsys):
    from wyoming.error import Error

    class Err(FakePhrase):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                await self.write_event(Error(text="boom").event())
                return True
            return await super().handle_event(event)

    result, _ = await _compare_run(tmp_path, Err)
    rec = _last_run(capsys)
    assert result.text == "heard 16000" and rec["phrase_error"] == "error"


@pytest.mark.asyncio
async def test_compare_hung_shadow_connect_does_not_delay_whisper(tmp_path, capsys, monkeypatch):
    # The shadow's connect never completes (a blackholed host). HA's answer must not wait.
    from dcv_stt_chain import handler as handler_mod

    class Hang:
        @staticmethod
        def from_uri(uri):
            real = AsyncClient.from_uri(uri)
            if "phrase-host" in uri:
                async def never():
                    await asyncio.sleep(30)

                real.connect = never
            return real

    monkeypatch.setattr(handler_mod, "AsyncClient", Hang)
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, mode="compare", phrase_uri="tcp://phrase-host:1", connect_timeout=2.0)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        t0 = time.monotonic()
        result = await run_command(pport, chunks=1)
        took = time.monotonic() - t0
        await asyncio.sleep(3.0)
    finally:
        await proxy.stop()
        await whisper.stop()
    rec = _last_run(capsys)
    assert result.text.startswith("heard") and took < 1.2  # 0.3 s is the test client's own settle
    assert rec["phrase_error"] == "connect: timeout"


@pytest.mark.asyncio
async def test_repeated_transcribe_does_not_leak_shadow_sockets(tmp_path):
    live = {"n": 0}

    class Counting(FakePhrase):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            live["n"] += 1

        async def disconnect(self):
            live["n"] -= 1

    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    phrase, pport_phrase = await start(lambda r, w: Counting(r, w))
    cfg = make_cfg(tmp_path, wport, mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport_phrase}")
    proxy, pport = await start(partial(SttChainHandler, cfg))
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
    await client.connect()
    try:
        for _ in range(5):
            await client.write_event(Transcribe(language="en").event())
            await asyncio.sleep(0.15)
        assert live["n"] <= 1  # only the latest run's shadow is open
    finally:
        await client.disconnect()
        await asyncio.sleep(0.5)
        assert live["n"] == 0
        await proxy.stop()
        await whisper.stop()
        await phrase.stop()


def test_agree_needs_real_words_and_handles_non_ascii():
    from dcv_stt_chain.handler import _norm

    assert _norm("Für Elise!") == "für elise"
    assert _norm("!!!") == ""


def test_shadow_queue_is_bounded_and_skips_empty_chunks():
    import dcv_stt_chain.handler as h

    class Dummy(SttChainHandler):
        def __init__(self):
            self.shadow_queue = asyncio.Queue()
            self.shadow_dead = False

    d = Dummy()
    d._send_shadow(AudioChunk(rate=16000, width=2, channels=1, audio=b"").event())
    assert d.shadow_queue.qsize() == 0  # empty chunks are not queued
    chunk = AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00").event()
    for _ in range(h._SHADOW_MAX_QUEUED + 50):
        d._send_shadow(chunk)
    assert d.shadow_queue.qsize() == h._SHADOW_MAX_QUEUED and d.shadow_dead is True
