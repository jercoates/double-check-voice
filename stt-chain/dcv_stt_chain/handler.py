"""Wyoming STT handler: pass-through to faster-whisper, logging every run."""

import asyncio
import json
import logging
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from urllib.parse import urlparse

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler

from . import audio_store
from .config import Config
from .decide import clean, decide, sentences_to_check

_LOGGER = logging.getLogger(__name__)

# What Home Assistant sends to STT engines.
_RATE, _WIDTH, _CHANNELS = 16000, 2, 1
# Bounds on what one network message may claim, checked before anything is read.
_MAX_DATA_BYTES = 16 * 1024
_MAX_PAYLOAD_BYTES = 64 * 1024
_UPSTREAM_MAX_DATA_BYTES = 64 * 1024  # a transcript is a few KB
_DESCRIBE_MAX_DATA_BYTES = 1024 * 1024  # an engine's Info lists every model it offers
_ABORT_WAIT_S = 1.0
_SHADOW_WRITE_S = 0.25  # the shadow engine may never slow the real path
_SHADOW_MAX_QUEUED = 2048  # events; past this the shadow is given up for the run
_LANG_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")  # client-chosen: validated before use
_SAVE_WAIT_S = 15.0  # how long a connection waits for the audio write before moving on
# Run-log writes get their own two threads: a hung share must not starve audio saves.
_RUNLOG_WAIT_S = 3.0  # short: the file is a convenience, never worth holding a slot
_RUNLOG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dcv-runlog")


def _norm(text: str) -> str:
    """Lower-case words only, so punctuation and case don't count as disagreement."""
    return " ".join(re.findall(r"[\w']+", text.lower()))


class ProtocolError(Exception):
    """A client message that is malformed or over the limits."""


async def read_capped(
    reader, timeout, max_data: int = _MAX_DATA_BYTES, max_payload: int = _MAX_PAYLOAD_BYTES
) -> Optional[Event]:
    """Read one Wyoming event, refusing anything over the limits before reading it.

    `timeout` is a function returning the seconds allowed for the next read. Used for
    clients and for the engines behind us: neither may make us buffer an unbounded
    message. Returns None when the peer closed the connection.
    """
    try:
        # A header line is bounded by asyncio's default 64 KB stream limit (wyoming sets none);
        # a longer one raises ValueError here and is answered like any other protocol error.
        line = await asyncio.wait_for(reader.readline(), timeout())
        if not line:
            return None
        header = json.loads(line)
        if not isinstance(header, dict) or not isinstance(header.get("type"), str):
            raise ProtocolError("bad header")
        data_len = header.get("data_length") or 0
        payload_len = header.get("payload_length") or 0
        if (
            not isinstance(data_len, int)
            or not isinstance(payload_len, int)
            or isinstance(data_len, bool)
            or isinstance(payload_len, bool)
            or not (0 <= data_len <= max_data)
            or not (0 <= payload_len <= max_payload)
        ):
            raise ProtocolError("bad length")
        data = header.get("data") or {}
        if data_len:
            extra = json.loads(await asyncio.wait_for(reader.readexactly(data_len), timeout()))
            if not isinstance(extra, dict) or not isinstance(data, dict):
                raise ProtocolError("bad data")
            data = {**data, **extra}
        if not isinstance(data, dict):
            raise ProtocolError("bad data")
        payload = None
        if payload_len:
            payload = await asyncio.wait_for(reader.readexactly(payload_len), timeout())
    except (ValueError, TypeError, RecursionError) as err:
        raise ProtocolError(type(err).__name__) from err
    return Event(type=header["type"], data=data, payload=payload)


def _text_of(event: Event) -> str:
    """The transcript text of an engine's reply, or "" if it is not a string."""
    text = Transcript.from_event(event).text
    return text if isinstance(text, str) else ""


async def read_upstream(client: AsyncClient, deadline: float, max_data: int = _UPSTREAM_MAX_DATA_BYTES):
    """Read one event from Whisper or Speech-to-Phrase, size-capped, until `deadline` (loop time)."""
    loop = asyncio.get_running_loop()
    return await read_capped(
        client._reader,  # noqa: SLF001 - wyoming's own reader; wyoming is pinned
        lambda: max(0.1, deadline - loop.time()),
        max_data=max_data,
        max_payload=_MAX_PAYLOAD_BYTES,
    )


def log_run(record: dict) -> None:
    """One JSON line per pipeline run, on stdout."""
    sys.stdout.write(json.dumps(record) + "\n")  # ASCII-only: no raw control/separator characters
    sys.stdout.flush()


class Shared:
    """Process-wide counters: open connections and audio bytes on disk."""

    def __init__(
        self, max_connections: int = 0, max_per_ip: int = 0, audio_bytes: int = 0, ha=None
    ) -> None:
        self.ha = ha  # HaChecker, or None when no decision is configured
        self.max_connections = max_connections
        self.max_per_ip = max_per_ip
        self.active = 0
        self.per_ip: dict = {}
        self.refusal_logged: dict = {}
        self.audio_bytes = audio_bytes
        # One purge at a time: a burst of saves at the cap must not each rescan the disk.
        self.purge_lock = asyncio.Lock()


async def close_client(client: Optional[AsyncClient]) -> None:
    """Close an upstream client without ever waiting on a peer that stopped reading."""
    if client is None:
        return
    writer = getattr(client, "_writer", None)
    transport = getattr(writer, "transport", None)
    if transport is not None:
        transport.abort()  # drop unsent bytes now; wait_closed() can otherwise hang
    try:
        await asyncio.wait_for(client.disconnect(), _ABORT_WAIT_S)
    except Exception:  # noqa: BLE001
        pass


class SttChainHandler(AsyncEventHandler):
    """One instance per HA connection; nothing is shared between connections."""

    def __init__(self, cfg: Config, reader, writer, shared: Optional[Shared] = None) -> None:
        super().__init__(reader, writer)
        self.cfg = cfg
        self.shared = shared or Shared()
        self.connected_at = time.monotonic()
        self.peer: Optional[str] = None
        self._reset()

    def _reset(self) -> None:
        self.run_id = uuid.uuid4().hex[:8]
        self.upstream: Optional[AsyncClient] = None
        self.upstream_error: Optional[str] = None
        self.language: Optional[str] = None
        self.in_run = False
        self.run_started = 0.0
        self.pcm = bytearray()
        self.odd_byte = b""
        self.truncated = False
        self.shadow_queue: Optional[asyncio.Queue] = None  # compare mode only
        self.shadow_task: Optional[asyncio.Future] = None
        self.shadow_dead = False

    # ---- connection loop -------------------------------------------------

    def _read_timeout(self) -> float:
        """Idle limit, shortened so a connection can never outlive max_connection_s."""
        left = self.connected_at + self.cfg.max_connection_s - time.monotonic()
        return max(0.1, min(self.cfg.idle_timeout, left))

    async def _read_event(self) -> Optional[Event]:
        """Read one event, refusing anything over the limits before reading it."""
        return await read_capped(self.reader, self._read_timeout)

    async def run(self) -> None:
        self._is_running = True
        s = self.shared
        peername = self.writer.get_extra_info("peername")
        self.peer = peername[0] if peername else None
        if self.peer:
            self.peer = self.peer.split("%")[0]  # drop an IPv6 scope such as fe80::1%eth0
            if self.peer.startswith("::ffff:"):
                self.peer = self.peer[7:]  # IPv4 client on a dual-stack socket
        # Loopback and the address the client actually reached are always allowed:
        # that is the container's own health check. The socket's own address is
        # used (not the configured host name), so host names and IPv6 forms work.
        sockname = self.writer.get_extra_info("sockname")
        own_ip = sockname[0].split("%")[0] if sockname else None
        if own_ip and own_ip.startswith("::ffff:"):
            own_ip = own_ip[7:]
        if (
            self.cfg.allowed_peers
            and self.peer not in self.cfg.allowed_peers
            and self.peer not in ("127.0.0.1", "::1", own_ip)
        ):
            self._warn_refusal(f"refusing connection from {self.peer} (not in DCV_ALLOWED_PEERS)")
            self.writer.close()
            return
        if (s.max_connections and s.active >= s.max_connections) or (
            s.max_per_ip and s.per_ip.get(self.peer, 0) >= s.max_per_ip
        ):
            self._warn_refusal(f"too many connections; refusing {self.peer}")
            self.writer.close()
            return
        s.active += 1
        s.per_ip[self.peer] = s.per_ip.get(self.peer, 0) + 1
        self.connected_at = time.monotonic()
        try:
            while self._is_running:
                try:
                    event = await self._read_event()
                    if event is None:
                        break
                    if not await self.handle_event(event):
                        break
                    if time.monotonic() - self.connected_at > self.cfg.max_connection_s:
                        _LOGGER.info("connection too long; closing")
                        await self._answer_if_in_run()
                        break
                except asyncio.TimeoutError:
                    _LOGGER.info("client idle too long; closing")
                    await self._answer_if_in_run()
                    break
                except ProtocolError as err:
                    _LOGGER.warning("closing connection: %s", type(err).__name__)
                    await self._answer_if_in_run()
                    break
                except (asyncio.IncompleteReadError, ConnectionError) as err:
                    _LOGGER.warning("closing connection: %s", type(err).__name__)
                    break
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error("handler error: %s", type(err).__name__)
                    break
        finally:
            s.active -= 1
            s.per_ip[self.peer] = s.per_ip.get(self.peer, 1) - 1
            if s.per_ip[self.peer] <= 0:
                s.per_ip.pop(self.peer, None)
            try:
                await self.disconnect()
            finally:
                self.writer.close()

    def _warn_refusal(self, message: str) -> None:
        """One warning per message per minute: a scanner must not flush the real logs."""
        now = time.monotonic()
        last = self.shared.refusal_logged.get(message, -1e9)
        if now - last >= 60:
            if len(self.shared.refusal_logged) > 256:
                self.shared.refusal_logged.clear()
            self.shared.refusal_logged[message] = now
            _LOGGER.warning(message)

    async def _answer_if_in_run(self) -> None:
        """We are closing on a command that is still open: answer with what we have."""
        if self.in_run:
            try:
                await self._finish()
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("could not finish run: %s", type(err).__name__)

    async def disconnect(self) -> None:
        """Client went away. If a command was in flight, keep its audio and log it."""
        if self.in_run:
            await self._record("", None, "client_disconnected")
        await self._close_upstream()
        await self._close_shadow()

    # ---- upstream --------------------------------------------------------

    async def _close_upstream(self) -> None:
        client, self.upstream = self.upstream, None
        await close_client(client)

    async def _open_upstream(self) -> None:
        if self._comparing:
            self._shadow_begin()  # runs in its own task: never waits on, or blocks, Whisper
        client = AsyncClient.from_uri(self.cfg.whisper_uri)
        await asyncio.wait_for(client.connect(), self.cfg.connect_timeout)
        self.upstream = client

    # ---- shadow (Speech-to-Phrase, compare mode only) --------------------
    # One background task per run owns the shadow socket and is fed through a queue.
    # Nothing here is awaited inline by the Whisper path, so a dead, slow or hung
    # shadow cannot delay HA's answer.

    @property
    def _comparing(self) -> bool:
        return self.cfg.mode == "compare" and bool(self.cfg.phrase_uri)

    def _shadow_begin(self) -> None:
        self.shadow_dead = False
        self.shadow_queue = asyncio.Queue()
        self.shadow_task = asyncio.ensure_future(self._shadow_run(self.shadow_queue))

    def _send_shadow(self, event: Event) -> None:
        if self.shadow_queue is None or self.shadow_dead:
            return
        if AudioChunk.is_type(event.type) and not (event.payload or b""):
            return  # an empty chunk carries nothing for the shadow
        if self.shadow_queue.qsize() >= _SHADOW_MAX_QUEUED:
            self.shadow_dead = True  # a flood the shadow can't drain: drop it, Whisper carries on
            return
        self.shadow_queue.put_nowait(event)

    def _shadow_stop(self, t_stop: float) -> None:
        if self.shadow_queue is not None and not self.shadow_dead:
            self.shadow_queue.put_nowait(t_stop)  # a float marks the end of the audio

    async def _close_shadow(self) -> None:
        task, self.shadow_task = self.shadow_task, None
        self.shadow_queue = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.wait({task}, timeout=_ABORT_WAIT_S + 0.5)
        if task.done() and not task.cancelled():
            task.exception()  # mark retrieved

    async def _shadow_run(self, queue: "asyncio.Queue") -> tuple:
        """Stream the run's audio to Speech-to-Phrase and ask for its text.

        Returns (text, ms, error). Never raises (except on cancel).
        """
        loop = asyncio.get_running_loop()
        client = None
        stage = "connect"
        text, ms, error = "", None, None
        try:
            client = AsyncClient.from_uri(self.cfg.phrase_uri)
            await asyncio.wait_for(client.connect(), self.cfg.connect_timeout)
            stage = "send"
            while True:
                item = await queue.get()
                if isinstance(item, float):
                    t_stop = item
                    break
                await asyncio.wait_for(client.write_event(item), _SHADOW_WRITE_S)
            deadline = t_stop + self.cfg.phrase_timeout
            await asyncio.wait_for(
                client.write_event(AudioStop().event()), max(0.1, deadline - loop.time())
            )
            stage = "read"
            while True:
                if loop.time() > deadline:
                    error = "timeout"
                    break
                event = await read_upstream(client, deadline)
                if event is None:
                    error = "closed"
                    break
                if Error.is_type(event.type):
                    error = "error"
                    break
                if Transcript.is_type(event.type):
                    text = _text_of(event)
                    ms = round((loop.time() - t_stop) * 1000)
                    break
        except asyncio.TimeoutError:
            error = "timeout" if stage == "read" else f"{stage}: timeout"
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            error = f"{stage}: {type(err).__name__}"
        finally:
            if self.shadow_queue is queue:  # not a newer run's queue, if this task ran late
                self.shadow_dead = True  # stop queueing; nothing reads the queue any more
            await close_client(client)
        return text, ms, error

    async def _send_upstream(self, event: Event) -> None:
        self._send_shadow(event)
        if self.upstream is None:
            return
        try:
            await asyncio.wait_for(self.upstream.write_event(event), self.cfg.connect_timeout)
        except Exception as err:  # noqa: BLE001
            self.upstream_error = f"send: {type(err).__name__}"
            await self._close_upstream()

    # ---- events ----------------------------------------------------------

    async def _empty_answer(self) -> None:
        try:
            await asyncio.wait_for(
                self.write_event(Transcript(text="").event()), self.cfg.connect_timeout
            )
        except Exception:  # noqa: BLE001
            pass

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            return await self._describe()

        if Transcribe.is_type(event.type):
            if self.in_run:
                return True  # late or repeated transcribe: ignore, keep the stream
            await self._close_upstream()  # a repeated transcribe must not orphan a socket
            await self._close_shadow()
            self._reset()
            lang = Transcribe.from_event(event).language
            self.language = lang if isinstance(lang, str) and _LANG_RE.fullmatch(lang) else None
            self.run_started = time.monotonic()
            try:
                await self._open_upstream()
            except Exception as err:  # noqa: BLE001
                self.upstream_error = f"connect: {type(err).__name__}"
            # Rebuilt from validated fields: nothing else the client sent reaches Whisper.
            await self._send_upstream(Transcribe(language=self.language).event())
            return True

        if AudioStart.is_type(event.type):
            if self.in_run:
                await self._finish()  # second audio-start without a stop: answer what we have
                return False
            try:
                start = AudioStart.from_event(event)
            except Exception:  # noqa: BLE001
                start = None
            if start is None or (start.rate, start.width, start.channels) != (
                _RATE,
                _WIDTH,
                _CHANNELS,
            ):
                _LOGGER.warning("unsupported audio-start format (%d fields)", len(event.data))
                await self._empty_answer()  # HA says "didn't understand", not an error
                return False
            if self.upstream is None and self.upstream_error is None:
                self._reset()  # HA skipped the optional transcribe
                self.run_started = time.monotonic()
                try:
                    await self._open_upstream()
                except Exception as err:  # noqa: BLE001
                    self.upstream_error = f"connect: {type(err).__name__}"
            self.run_started = time.monotonic()  # the clock for "a command that never ends"
            self.in_run = True
            await self._send_upstream(
                AudioStart(rate=_RATE, width=_WIDTH, channels=_CHANNELS).event()
            )
            return True

        if AudioChunk.is_type(event.type):
            if not self.in_run:
                await self._empty_answer()  # chunk before audio-start: protocol error
                return False
            if time.monotonic() - self.run_started > self.cfg.max_capture_s + self.cfg.idle_timeout:
                await self._finish()  # a command that never ends: answer with what we have
                return False
            # 16-bit samples are two bytes: an odd chunk would shift every later sample,
            # so the stray byte waits for the next chunk instead of being forwarded.
            payload = self.odd_byte + (event.payload or b"")
            self.odd_byte = payload[-1:] if len(payload) % 2 else b""
            if self.odd_byte:
                payload = payload[:-1]
            if not payload:
                return True  # only the carried byte so far: nothing whole to forward yet
            limit = int(self.cfg.max_capture_s * _RATE * _WIDTH * _CHANNELS)
            if self.truncated or len(self.pcm) + len(payload) > limit:
                # Over the cap: stop buffering AND forwarding; Whisper gets the first part.
                self.truncated = True
                return True
            self.pcm.extend(payload)
            await self._send_upstream(
                AudioChunk(rate=_RATE, width=_WIDTH, channels=_CHANNELS, audio=payload).event()
            )
            return True

        if AudioStop.is_type(event.type):
            if not self.in_run:
                await self._empty_answer()  # stop with no start: never leave HA waiting
                return True
            await self._finish()
            return True

        return True

    async def _describe(self) -> bool:
        """Relay the engine's Info so HA sees the real capabilities.

        If Whisper is down, close without answering: the health check then
        shows unhealthy instead of an open port.
        """
        client = AsyncClient.from_uri(self.cfg.whisper_uri)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.cfg.connect_timeout
            await asyncio.wait_for(client.connect(), self.cfg.connect_timeout)
            await asyncio.wait_for(
                client.write_event(Describe().event()), max(0.1, deadline - loop.time())
            )
            while True:
                event = await read_upstream(client, deadline + 1.0, _DESCRIBE_MAX_DATA_BYTES)
                if event is None:
                    return False
                if Info.is_type(event.type):
                    # Report our own name, so HA shows this engine apart from Whisper itself.
                    info = Info.from_event(event)
                    for program in info.asr:
                        program.name = f"dcv-stt-chain-{self.cfg.device}"
                    await asyncio.wait_for(
                        self.write_event(info.event()), self.cfg.connect_timeout
                    )
                    return True
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("describe failed: %s", type(err).__name__)
            return False
        finally:
            await close_client(client)

    async def _finish(self) -> None:
        """Audio is complete: get Whisper's text under one deadline, answer HA, record."""
        loop = asyncio.get_running_loop()
        t_stop = loop.time()
        deadline = t_stop + self.cfg.result_timeout
        text = ""
        upstream_ms = None
        error = self.upstream_error
        # Raw audio goes to disk before Whisper is asked anything; it runs alongside
        # the decode so a slow disk cannot delay the answer.
        save_task = asyncio.ensure_future(self._save_audio())
        self._shadow_stop(t_stop)
        shadow_task = self.shadow_task
        phrase = None
        try:
            if self.upstream is not None:
                try:
                    await asyncio.wait_for(
                        self.upstream.write_event(AudioStop().event()),
                        max(0.1, deadline - loop.time()),
                    )
                    while True:
                        if loop.time() > deadline:
                            error = "timeout"  # chatty upstream must not extend the deadline
                            break
                        event = await read_upstream(self.upstream, deadline)
                        if event is None:
                            error = "upstream closed"
                            break
                        if Error.is_type(event.type):
                            error = "upstream error"
                            break
                        if Transcript.is_type(event.type):
                            text = _text_of(event)
                            upstream_ms = round((loop.time() - t_stop) * 1000)
                            break
                except asyncio.TimeoutError:
                    error = "timeout"
                except Exception as err:  # noqa: BLE001
                    error = f"read: {type(err).__name__}"
            await self._close_upstream()

            # Always answer HA, even if empty: never hang the pipeline.
            try:
                await asyncio.wait_for(
                    self.write_event(Transcript(text=text, language=self.language).event()),
                    self.cfg.connect_timeout,
                )
            except Exception as err:  # noqa: BLE001
                error = f"{error + '; ' if error else ''}answer: {type(err).__name__}"
        finally:
            saved = await self._wait_saved(save_task)
            if shadow_task is not None:
                # Held at most until just past the shadow's own deadline, counted from the stop.
                left = max(0.1, t_stop + self.cfg.phrase_timeout + 0.5 - loop.time())
                try:
                    phrase = await asyncio.wait_for(shadow_task, left)
                except Exception:  # noqa: BLE001
                    phrase = ("", None, "timeout")
            decision = await self._decide(text, phrase)
            await self._record(text, upstream_ms, error, saved, phrase, decision)
            await self._close_shadow()
            self._reset()

    async def _decide(self, whisper_text: str, phrase: Optional[tuple]) -> Optional[dict]:
        """Shadow decision: asks Home Assistant what it can act on and logs which text it would
        have used. Never used for the answer; never raises; bounded by the check's timeout."""
        checker = self.shared.ha
        if checker is None or self.cfg.decision == "off":
            return None
        try:
            # Cleaned first (single spaces, bounded length): a crafted transcript must not be
            # able to make the pattern matching below slow, and the loop must never stall.
            whisper_text = clean(whisper_text)
            phrase_text = clean(phrase[0]) if phrase else ""
            asked = sentences_to_check(whisper_text, phrase_text)
            t0 = time.monotonic()
            results = (
                await asyncio.wait_for(
                    checker.check(asked, self.language), self.cfg.ha_timeout + 0.5
                )
                if asked
                else []
            )
            out = decide(whisper_text, phrase_text, results, asked)
            out["ha_ms"] = round((time.monotonic() - t0) * 1000)
            return out
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("decision failed: %s", type(err).__name__)
            return None

    @staticmethod
    async def _wait_saved(save_task) -> tuple:
        """HA already has its answer; a stalled disk must not pin this connection."""
        try:
            return await asyncio.wait_for(asyncio.shield(save_task), _SAVE_WAIT_S)
        except Exception:  # noqa: BLE001
            # The write may still land later; the file is named by its run id.
            return (None, "audio_save_slow")

    async def _save_audio(self) -> tuple:
        """Save the raw audio as WAV. Returns (path or None, error or None). Never raises."""
        wav = None
        audio_error = None
        # Snapshot first: if this outlives the caller's wait, _reset() must not change it.
        pcm = bytes(self.pcm)
        run_id = self.run_id
        try:
            if pcm:
                size = max(len(pcm) + 44, audio_store.MIN_FILE_COST)
                cap = self.cfg.audio_max_mb * 1024 * 1024
                loop = asyncio.get_running_loop()
                if cap and self.shared.audio_bytes + size > cap:
                    # Make room by deleting the oldest audio, so a flood of junk clips
                    # can never stop the newest real commands from being kept. One purge
                    # at a time: the others wait, then find the room already made.
                    async with self.shared.purge_lock:
                        if self.shared.audio_bytes + size > cap:
                            await loop.run_in_executor(
                                audio_store.IO_POOL,
                                audio_store.purge_old,
                                self.cfg.audio_dir,
                                0,
                                # to 90% of the cap, so a full store is not rescanned on every save
                                max(1, int(cap * 0.9) - size),  # never 0: that means no limit
                            )
                            self.shared.audio_bytes = await loop.run_in_executor(
                                audio_store.IO_POOL, audio_store.dir_size, self.cfg.audio_dir
                            )
                if cap and self.shared.audio_bytes + size > cap:
                    audio_error = "audio_cap_reached"  # cap smaller than one clip
                else:
                    wav = await loop.run_in_executor(
                        audio_store.IO_POOL,
                        audio_store.save_wav,
                        self.cfg.audio_dir,
                        self.cfg.device,
                        run_id,
                        pcm,
                        _RATE,
                        _WIDTH,
                        _CHANNELS,
                    )
                    if wav is None:
                        audio_error = "audio_save_failed"
                    else:
                        self.shared.audio_bytes += size
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("could not save audio: %s", type(err).__name__)
            audio_error = "audio_save_failed"
        return wav, audio_error

    def _profile(self) -> dict:
        """The settings this run used, so it can be replayed."""
        profile = {"whisper_uri": self.cfg.whisper_uri}
        if self._comparing:
            profile["phrase_uri"] = self.cfg.phrase_uri
            profile["phrase_timeout"] = self.cfg.phrase_timeout
        return profile

    async def _record(
        self,
        text: str,
        upstream_ms: Optional[int],
        error: Optional[str],
        saved: Optional[tuple] = None,
        phrase: Optional[tuple] = None,
        decision: Optional[dict] = None,
    ) -> None:
        """Write the run's log line (saving the audio first if it isn't saved yet)."""
        self.in_run = False
        try:
            if saved is None:
                saved = await self._wait_saved(asyncio.ensure_future(self._save_audio()))
            wav, audio_error = saved
            record = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "run": self.run_id,
                "device": self.cfg.device,
                "peer": self.peer,
                "mode": self.cfg.mode,
                "profile": self._profile(),
                "language": self.language,
                "capture_s": round(len(self.pcm) / (_RATE * _WIDTH * _CHANNELS), 2),
                "audio_truncated": self.truncated,
                "whisper_text": text,
                "used": "whisper" if text else "none",
                "latency_ms": {"whisper_after_stop": upstream_ms},
                "error": error,
                "audio_error": audio_error,
                "audio": str(wav) if wav else None,
            }
            if phrase is not None:
                p_text, p_ms, p_err = phrase
                record["phrase_text"] = p_text
                record["phrase_matched"] = bool(p_text)
                record["phrase_error"] = p_err
                record["latency_ms"]["phrase_after_stop"] = p_ms
                record["agree"] = bool(_norm(p_text) and _norm(p_text) == _norm(text))
            if decision is not None:
                record["decision"] = decision
            log_run(record)
            # Skipped when the audio write already stalled: the disk is the problem,
            # and this connection must not wait on it a second time.
            # Runs with no audio (a client looping start/stop) stay out of the file.
            if self.cfg.run_log_file and self.pcm and audio_error != "audio_save_slow":
                # Same line, kept next to the audio so a run and its clip can be read together.
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        _RUNLOG_POOL,
                        audio_store.append_run_log,
                        self.cfg.audio_dir,
                        self.cfg.device,
                        json.dumps(record),
                        wav.parent if wav else None,
                    ),
                    _RUNLOG_WAIT_S,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("could not record run: %s", type(err).__name__)
