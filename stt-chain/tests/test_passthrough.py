"""End-to-end tests: client -> proxy -> fake Whisper, all over real TCP sockets."""

import asyncio
import socket
import wave
from functools import partial
from pathlib import Path

import pytest
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

from dcv_stt_chain.config import Config
from dcv_stt_chain.handler import SttChainHandler

ATTR = Attribution(name="t", url="http://t")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeWhisper(AsyncEventHandler):
    """Answers describe; on audio-stop returns a transcript of how many bytes it heard."""

    def __init__(self, *args, delay: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self.nbytes = 0

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            info = Info(
                asr=[
                    AsrProgram(
                        name="fake",
                        description="fake",
                        attribution=ATTR,
                        installed=True,
                        version="1",
                        models=[
                            AsrModel(
                                name="m",
                                description="m",
                                attribution=ATTR,
                                installed=True,
                                languages=["en"],
                                version="1",
                            )
                        ],
                    )
                ]
            )
            await self.write_event(info.event())
        elif AudioChunk.is_type(event.type):
            self.nbytes += len(event.payload or b"")
        elif AudioStop.is_type(event.type):
            await asyncio.sleep(self.delay)
            await self.write_event(Transcript(text=f"heard {self.nbytes}").event())
        return True


async def start(handler_factory):
    port = free_port()
    server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
    await server.start(handler_factory)
    return server, port


def make_cfg(tmp_path: Path, whisper_port: int, **over) -> Config:
    base = dict(
        listen_uri="tcp://127.0.0.1:0",
        whisper_uri=f"tcp://127.0.0.1:{whisper_port}",
        device="test-dev",
        audio_dir=tmp_path / "audio",
        audio_retention_days=30,
        audio_max_mb=100,
        connect_timeout=1.0,
        result_timeout=1.0,
        idle_timeout=5.0,
        max_capture_s=30.0,
        max_connections=16,
        max_per_ip=0,
        max_connection_s=60.0,
    )
    base.update(over)
    return Config(**base)


async def run_command(proxy_port: int, chunks: int = 5, size: int = 3200) -> Transcript:
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{proxy_port}")
    await client.connect()
    try:
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        for _ in range(chunks):
            await client.write_event(
                AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * (size // 2)).event()
            )
        await client.write_event(AudioStop().event())
        event = await asyncio.wait_for(client.read_event(), 5)
        await asyncio.sleep(0.3)  # the proxy answers HA first, then saves audio and logs
        return Transcript.from_event(event)
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_passthrough_returns_whisper_text_and_saves_wav(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"
    wavs = list((tmp_path / "audio").glob("*/*.wav"))
    assert len(wavs) == 1 and "test-dev" in wavs[0].name
    with wave.open(str(wavs[0])) as w:
        assert w.getnframes() == 8000 and w.getframerate() == 16000
    log = capsys.readouterr().out.strip().splitlines()
    assert len(log) == 1 and '"whisper_text": "heard 16000"' in log[0]


@pytest.mark.asyncio
async def test_whisper_down_returns_empty_transcript_fast_and_still_saves_audio(tmp_path, capsys):
    cfg = make_cfg(tmp_path, free_port())  # nothing listening there
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
    assert result.text == ""
    assert len(list((tmp_path / "audio").glob("*/*.wav"))) == 1
    assert '"error": "connect:' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_whisper_slow_times_out_with_empty_transcript(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w, delay=3.0))
    cfg = make_cfg(tmp_path, wport, result_timeout=0.3)
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        result = await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == ""


@pytest.mark.asyncio
async def test_describe_relays_info_and_fails_closed_when_whisper_down(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(Describe().event())
        info = Info.from_event(await asyncio.wait_for(client.read_event(), 5))
        assert info.asr[0].name == "dcv-stt-chain-test-dev"  # our name, not Whisper's
        assert info.asr[0].models[0].name == "m"  # everything else passes through
        await client.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()

    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, free_port())))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(Describe().event())
        assert await asyncio.wait_for(client.read_event(), 5) is None  # closed, no Info
        await client.disconnect()
    finally:
        await proxy.stop()


@pytest.mark.asyncio
async def test_two_connections_do_not_cross(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        a, b = await asyncio.gather(
            run_command(pport, chunks=2), run_command(pport, chunks=7)
        )
    finally:
        await proxy.stop()
        await whisper.stop()
    assert (a.text, b.text) == ("heard 6400", "heard 22400")


@pytest.mark.asyncio
async def test_bad_audio_format_gets_empty_transcript_then_closes(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    proxy, pport = await start(partial(SttChainHandler, make_cfg(tmp_path, wport)))
    try:
        client = AsyncClient.from_uri(f"tcp://127.0.0.1:{pport}")
        await client.connect()
        await client.write_event(AudioStart(rate=16000, width=2, channels=99).event())
        answer = await asyncio.wait_for(client.read_event(), 5)
        assert Transcript.from_event(answer).text == ""  # HA says "didn't understand"
        assert await asyncio.wait_for(client.read_event(), 5) is None
        await client.disconnect()
    finally:
        await proxy.stop()
        await whisper.stop()


@pytest.mark.asyncio
async def test_capture_cap_limits_saved_audio_but_still_transcribes(tmp_path):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport, max_capture_s=0.5)  # 16000 bytes
    proxy, pport = await start(partial(SttChainHandler, cfg))
    try:
        result = await run_command(pport, chunks=10)  # 32000 bytes
    finally:
        await proxy.stop()
        await whisper.stop()
    assert result.text == "heard 16000"  # cap 0.5 s: only the first 16000 bytes go on
    with wave.open(str(next((tmp_path / "audio").glob("*/*.wav")))) as w:
        assert w.getnframes() <= 8000
