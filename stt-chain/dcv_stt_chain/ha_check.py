"""Ask Home Assistant whether a sentence is a command it understands.

Uses Home Assistant's websocket "debug" call for the built-in conversation agent,
which only reports whether (and as which intent) a sentence would match: it never
runs anything. Everything here is best effort: any problem returns None and the
caller carries on as if the check did not exist.
"""

import asyncio
import json
import logging
import time
from typing import Optional

import websockets.asyncio.client as _wsc
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# Never follow a redirect: the token is sent right after the handshake, so it must only ever
# go to the address the operator configured. (The library's default is to follow up to 10.)
_wsc.MAX_REDIRECTS = 1

_LOGGER = logging.getLogger(__name__)

MAX_SENTENCES = 8
MAX_SENTENCE_CHARS = 512
MAX_MESSAGE_BYTES = 1024 * 1024  # a reply for 8 short sentences is a few KB
MAX_TOKEN_CHARS = 4096
_BACKOFF_START_S = 5.0
_BACKOFF_MAX_S = 60.0
_BACKOFF_AUTH_S = 300.0  # a rejected token must not be retried quickly: Home Assistant can ban repeat offenders
_ENTITY_SLOTS = ("name", "area", "floor", "domain", "device_class")


class AuthRejected(Exception):
    """Home Assistant said the token is not valid."""


class Refused(Exception):
    """Home Assistant answered this one request with an error; the connection is fine."""


class Paused(Exception):
    """A call that was queued behind a failing one: the pause started while it waited."""


def read_token(path: str) -> str:
    """The token from its file. Raises ValueError (never echoing the content) if unusable."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_TOKEN_CHARS + 2)  # never read a huge file whole
    except OSError as err:
        raise ValueError(f"cannot read the token file ({type(err).__name__})") from None
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise ValueError("the token file is not plain ASCII text (no byte-order mark, no other encoding)") from None
    if not token or len(token) > MAX_TOKEN_CHARS or any(c.isspace() or ord(c) < 33 for c in token):
        raise ValueError("the token file is empty or does not hold a single token")
    return token


class HaChecker:
    """One shared websocket to Home Assistant, opened on demand and reopened after a failure."""

    def __init__(self, url: str, token_file: str, timeout: float, token: Optional[str] = None) -> None:
        self.url = url
        self.token_file = token_file
        self.timeout = timeout
        self._token = token  # read once at start-up; re-read only after Home Assistant rejects it
        self._ws = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._down_until = 0.0
        self._backoff = _BACKOFF_START_S

    async def close(self) -> None:
        """Shut down politely (used at exit)."""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), 1.0)
            except Exception:  # noqa: BLE001
                await _abort(ws)

    async def _drop(self) -> None:
        """Tear the connection down NOW. A peer that stopped answering never completes a polite
        close, and waiting for it would outlast the caller's own limit."""
        ws, self._ws = self._ws, None
        if ws is not None:
            await _abort(ws)

    def _note_failure(self, seconds: Optional[float] = None) -> None:
        """Start (or lengthen) the pause, once per incident however many runs hit it."""
        now = time.monotonic()
        if now < self._down_until:
            return  # another run already reported this outage
        if seconds is None:
            seconds = self._backoff
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX_S)
        self._down_until = now + seconds

    async def check(self, sentences: list, language: Optional[str]) -> Optional[list]:
        """For each sentence: {"match": bool, "actionable": bool, "intent": str or None}.

        None if the check could not be done. Never raises, and never takes longer than the timeout.
        """
        sentences = [s[:MAX_SENTENCE_CHARS] for s in sentences[:MAX_SENTENCES]]
        if not sentences:
            return []
        if time.monotonic() < self._down_until:
            return None
        progress = {"in_call": False}
        try:
            return await asyncio.wait_for(self._locked_call(sentences, language, progress), self.timeout)
        except Paused:
            pass
        except Refused:
            _LOGGER.warning("Home Assistant refused the check for this run")
        except AuthRejected:
            self._token = None  # a replaced token file is picked up at the next try
            if time.monotonic() >= self._down_until:
                _LOGGER.error("Home Assistant rejected the token; not retrying for %d s", _BACKOFF_AUTH_S)
            self._note_failure(_BACKOFF_AUTH_S)
        except asyncio.CancelledError:
            if progress["in_call"]:
                self._note_failure()  # cancelled from outside while talking to Home Assistant
            raise
        except Exception as err:  # noqa: BLE001 - includes the timeout
            if progress["in_call"] or not isinstance(err, TimeoutError):
                if time.monotonic() >= self._down_until:
                    _LOGGER.warning("Home Assistant check failed: %s", type(err).__name__)
                self._note_failure()
            # else: only waited for the lock too long behind other runs; Home Assistant is not at fault
        return None

    async def _locked_call(self, sentences: list, language: Optional[str], progress: dict) -> list:
        async with self._lock:
            if time.monotonic() < self._down_until:
                raise Paused()  # queued behind a call that just failed: do not reconnect now
            progress["in_call"] = True
            try:
                for attempt in (1, 2):
                    reused = self._ws is not None
                    if self._ws is None:
                        self._ws = await self._open()
                    try:
                        return await self._exchange(sentences, language)
                    except ConnectionClosed:
                        await self._drop()
                        if not reused or attempt == 2:
                            raise
                        # an idle connection that Home Assistant or a router dropped: reconnect once
            except Refused:
                raise  # this request only; the connection stays
            except BaseException:
                await self._drop()  # a half-finished exchange must not leak into the next one
                raise
        raise RuntimeError("unreachable")

    async def _exchange(self, sentences: list, language: Optional[str]) -> list:
        self._next_id += 1
        request_id = self._next_id
        await self._ws.send(
            json.dumps(
                {
                    "id": request_id,
                    "type": "conversation/agent/homeassistant/debug",
                    "sentences": sentences,
                    "language": language or "en",
                }
            )
        )
        while True:
            msg = json.loads(await self._ws.recv())
            if isinstance(msg, dict) and msg.get("id") == request_id and msg.get("type") == "result":
                break
        if not msg.get("success"):
            raise Refused()
        results = (msg.get("result") or {}).get("results")
        if not isinstance(results, list) or len(results) != len(sentences):
            raise ValueError("unexpected reply shape")
        self._backoff = _BACKOFF_START_S
        return [_summary(r) for r in results]

    async def _open(self):
        if self._token is None:
            self._token = await asyncio.to_thread(read_token, self.token_file)  # never block the loop
        ws = await connect(
            self.url,
            max_size=MAX_MESSAGE_BYTES,
            open_timeout=self.timeout,
            proxy=None,  # never through an environment proxy
            close_timeout=1.0,
            compression=None,  # nothing to gain on a few KB, and one less thing to attack
            max_queue=16,
        )
        try:
            first = json.loads(await ws.recv())
            if not isinstance(first, dict) or first.get("type") != "auth_required":
                raise ValueError("unexpected greeting")
            await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
            answer = json.loads(await ws.recv())
            if not isinstance(answer, dict) or answer.get("type") != "auth_ok":
                raise AuthRejected()
        except BaseException:
            await _abort(ws)
            raise
        return ws


async def _abort(ws) -> None:
    """Drop the transport without the closing handshake, and wait only briefly for it to finish."""
    try:
        ws.transport.abort()
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.wait_for(ws.wait_closed(), 0.2)
    except Exception:  # noqa: BLE001
        pass


def _summary(result) -> dict:
    """Keep only what the decision uses.

    "actionable" is stricter than Home Assistant's own `match` flag: for a sentence that
    names a device, area, floor or kind of device, `match` is also true when the name belongs
    to something NOT exposed to voice (measured on a dev HA), and then `targets` is empty and
    nothing would happen. So such a sentence only counts if it resolved to at least one
    target. Sentences that name nothing (a timer, "never mind") count when matched.
    """
    if not isinstance(result, dict):
        return {"match": False, "actionable": False, "intent": None}
    intent = result.get("intent")
    name = intent.get("name") if isinstance(intent, dict) else None
    matched = result.get("match") is True
    slots = result.get("slots") if isinstance(result.get("slots"), dict) else {}
    targets = result.get("targets") if isinstance(result.get("targets"), dict) else {}
    names_something = any(k in slots for k in _ENTITY_SLOTS)
    return {
        "match": matched,
        "actionable": matched and (bool(targets) or not names_something),
        "intent": name if isinstance(name, str) and len(name) <= 64 else None,
    }
