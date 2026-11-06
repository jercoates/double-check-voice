"""The Home Assistant check and the shadow decision, against a fake Home Assistant websocket."""

import asyncio
import json
import time
from functools import partial

import pytest
from websockets.asyncio.server import serve

from dcv_stt_chain import ha_check
from dcv_stt_chain.config import Config
from dcv_stt_chain.decide import decide, pieces, sentences_to_check
from dcv_stt_chain.ha_check import HaChecker
from dcv_stt_chain.handler import Shared, SttChainHandler
from test_passthrough import FakeWhisper, free_port, make_cfg, run_command, start
from test_robustness import FakePhrase, _last_run, _phrase_cls

TOKEN = "t0ken-for-tests-only"


class FakeHa:
    """Home Assistant's websocket: auth, then the conversation debug call. Records what it saw."""

    def __init__(self, known=("turn the kitchen light off", "turn the kitchen light on"), unexposed=("turn on the hall light",), **mode):
        self.known = set(known)
        self.unexposed = set(unexposed)
        self.mode = mode  # auth_bad, slow, junk, big, refuse
        self.sentences_seen = []
        self.connections = 0
        self.tokens = []
        self.server = None
        self.port = None

    async def handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "test"}))
        auth = json.loads(await ws.recv())
        self.tokens.append(auth.get("access_token"))
        if self.mode.get("auth_bad") or auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "no"}))
            return
        await ws.send(json.dumps({"type": "auth_ok"}))
        replies = 0
        async for raw in ws:
            msg = json.loads(raw)
            self.sentences_seen.append(msg.get("sentences"))
            replies += 1
            if self.mode.get("slow"):
                await asyncio.sleep(self.mode["slow"])
            if self.mode.get("refuse"):
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": False, "error": {}}))
                continue
            if self.mode.get("big"):
                await ws.send("x" * (ha_check.MAX_MESSAGE_BYTES + 10))
                continue
            if self.mode.get("junk_after") and replies > self.mode["junk_after"]:
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": {"results": 5}}))
                continue
            if self.mode.get("junk"):
                await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": {"results": 5}}))
                continue
            results = []
            for s in msg["sentences"]:
                norm = " ".join(s.lower().replace(".", "").replace("?", "").split())
                if norm in self.known:
                    results.append(
                        {
                            "intent": {"name": "HassTurnOff" if "off" in norm else "HassTurnOn"},
                            "match": True,
                            "slots": {"name": "kitchen light"},
                            "targets": {"light.kitchen": {"matched": True}},
                        }
                    )
                elif norm in self.unexposed:  # real HA: a name it knows but that is not exposed
                    results.append(
                        {"intent": {"name": "HassTurnOn"}, "match": True, "slots": {"name": "hall light"}, "targets": {}}
                    )
                elif "nevermind" in norm:
                    results.append({"intent": {"name": "HassNevermind"}, "match": True, "slots": {}, "targets": {}})
                else:
                    results.append({"intent": {"name": "HassTurnOn"}, "match": False, "unmatched_slots": {"name": s}})
            await ws.send(json.dumps({"id": msg["id"], "type": "result", "success": True, "result": {"results": results}}))
            if self.mode.get("drop_after") and replies >= self.mode["drop_after"]:
                await ws.close()  # an idle connection dropped by Home Assistant or a router
                return

    async def start(self):
        self.server = await serve(self.handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}/api/websocket"


@pytest.fixture
def token_file(tmp_path):
    p = tmp_path / "token"
    p.write_text(TOKEN + "\n")
    return str(p)


# ---- the pure decision ---------------------------------------------------------


def _results(asked, ok):
    return [
        {"match": s in ok, "actionable": s in ok, "intent": "HassTurnOff" if s in ok else None} for s in asked
    ]


def test_whisper_wins_when_home_assistant_can_act_on_it():
    w, p = "Turn the kitchen light off.", "turn the Kitchen Light off"
    d = decide(w, p, _results(sentences_to_check(w, p), {w, p}))
    assert d["use"] == "whisper" and d["text"] == w and d["enforced"] is False


def test_a_command_is_picked_out_of_background_speech():
    w = "I don't kill a battery overnight.  Turn the kitchen light off.  Too much?"
    p = ""
    asked = sentences_to_check(w, p)
    assert "Turn the kitchen light off." in asked
    d = decide(w, p, _results(asked, {"Turn the kitchen light off."}))
    assert d["use"] == "whisper_trimmed" and d["text"] == "Turn the kitchen light off."


def test_speech_to_phrase_is_used_when_whisper_has_nothing_usable():
    w, p = "Turn the kitchen lid off.", "turn the Kitchen Light off"
    d = decide(w, p, _results(sentences_to_check(w, p), {p}))
    assert d["use"] == "phrase" and d["text"] == p


def test_whisper_text_is_kept_when_nothing_is_usable_or_the_check_failed():
    w, p = "Hmm what.", ""
    d = decide(w, p, _results(sentences_to_check(w, p), set()))
    assert d["use"] == "whisper_raw" and d["text"] == w
    d = decide(w, p, None)
    assert d["use"] == "whisper_raw" and d["text"] == w and d["ha"]["available"] is False


def test_pieces_and_limits():
    assert pieces("Turn it on. Turn it off.") == ["Turn it on.", "Turn it off."]
    assert pieces("turn the kitchen light on turn the kitchen light") == [
        "turn the kitchen light on",
        "turn the kitchen light",
    ]
    assert len(pieces(" ".join(["Hello."] * 50))) == 6
    assert len(sentences_to_check("a. " * 50, "b")) <= 8
    assert sentences_to_check("", "") == []


# ---- the checker ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_checker_reports_which_sentences_home_assistant_understands(token_file):
    ha = await FakeHa().start()
    checker = HaChecker(ha.url, token_file, 2.0)
    try:
        out = await checker.check(["Turn the kitchen light off.", "gibberish", "Nevermind"], "en")
    finally:
        await checker.close()
        await ha.stop()
    assert out == [
        {"match": True, "actionable": True, "intent": "HassTurnOff"},
        {"match": False, "actionable": False, "intent": "HassTurnOn"},
        {"match": True, "actionable": True, "intent": "HassNevermind"},  # names nothing: matched is enough
    ]
    assert ha.tokens == [TOKEN]


@pytest.mark.asyncio
async def test_checker_reuses_one_connection_and_limits_what_it_sends(token_file):
    ha = await FakeHa().start()
    checker = HaChecker(ha.url, token_file, 2.0)
    try:
        await checker.check(["a"], "en")
        await checker.check(["b" * 5000] * 20, "en")
    finally:
        await checker.close()
        await ha.stop()
    assert ha.connections == 1
    assert len(ha.sentences_seen[1]) == ha_check.MAX_SENTENCES
    assert all(len(s) == ha_check.MAX_SENTENCE_CHARS for s in ha.sentences_seen[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [{"junk": True}, {"refuse": True}, {"big": True}, {"slow": 3}])
async def test_checker_returns_none_on_any_failure_and_does_not_hammer(token_file, mode):
    ha = await FakeHa(**mode).start()
    checker = HaChecker(ha.url, token_file, 0.6)
    try:
        t0 = time.monotonic()
        assert await checker.check(["turn the kitchen light off"], "en") is None
        assert time.monotonic() - t0 < 1.5
        n = ha.connections
        assert await checker.check(["turn the kitchen light off"], "en") is None  # backing off
        assert ha.connections == n
    finally:
        await checker.close()
        await ha.stop()


@pytest.mark.asyncio
async def test_a_rejected_token_is_not_retried_quickly(tmp_path):
    bad = tmp_path / "bad"
    bad.write_text("wrong-token")
    ha = await FakeHa().start()
    checker = HaChecker(ha.url, str(bad), 1.0)
    try:
        assert await checker.check(["x"], "en") is None
        assert await checker.check(["x"], "en") is None
    finally:
        await checker.close()
        await ha.stop()
    assert ha.connections == 1  # the second call did not even connect
    assert checker._down_until - time.monotonic() > 200


@pytest.mark.asyncio
async def test_checker_with_home_assistant_down_returns_none_fast(token_file):
    from test_passthrough import free_port

    checker = HaChecker(f"ws://127.0.0.1:{free_port()}/api/websocket", token_file, 1.0)
    t0 = time.monotonic()
    assert await checker.check(["x"], "en") is None
    assert time.monotonic() - t0 < 2.0


def test_read_token_validation(tmp_path):
    ok = tmp_path / "ok"
    ok.write_text("  abc.def-123  \n")
    assert ha_check.read_token(str(ok)) == "abc.def-123"
    for bad in ("", "two words", "line1\nline2", "x" * 5000):
        f = tmp_path / "bad"
        f.write_text(bad)
        with pytest.raises(ValueError) as err:
            ha_check.read_token(str(f))
        assert bad.strip() == "" or bad[:6] not in str(err.value)  # never echoes the content
    with pytest.raises(ValueError):
        ha_check.read_token(str(tmp_path / "missing"))


# ---- in the proxy ----------------------------------------------------------------


async def _run(tmp_path, token_file, ha_mode=None, phrase=None, whisper_text=None, **over):
    ha = await FakeHa(**(ha_mode or {})).start()
    whisper_cls = FakeWhisper
    if whisper_text is not None:
        class W(FakeWhisper):
            async def handle_event(self, event):
                from wyoming.asr import Transcript
                from wyoming.audio import AudioStop

                if AudioStop.is_type(event.type):
                    await self.write_event(Transcript(text=whisper_text).event())
                    return True
                return await super().handle_event(event)

        whisper_cls = W
    whisper, wport = await start(lambda r, w: whisper_cls(r, w))
    extra = {}
    pserver = None
    if phrase is not None:
        pserver, pport = await start(lambda r, w: phrase(r, w))
        extra = dict(mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}")
    cfg = make_cfg(
        tmp_path, wport, ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        **{"decision": "shadow", **extra, **over},
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, pport2 = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    t0 = time.monotonic()
    try:
        result = await run_command(pport2)
        took = time.monotonic() - t0
        await asyncio.sleep(1.0)
    finally:
        await proxy.stop()
        await whisper.stop()
        if pserver:
            await pserver.stop()
        await checker.close()
        await ha.stop()
    return result, took, ha


@pytest.mark.asyncio
async def test_shadow_decision_is_logged_and_never_changes_the_answer(tmp_path, token_file, capsys):
    result, _, ha = await _run(
        tmp_path, token_file, whisper_text="I hate this. Turn the kitchen light off. Why?",
        phrase=_phrase_cls(text="turn the kitchen light off"),
    )
    rec = _last_run(capsys)
    assert result.text == "I hate this. Turn the kitchen light off. Why?"  # HA still gets Whisper's text
    d = rec["decision"]
    assert d["use"] == "whisper_trimmed" and d["text"] == "Turn the kitchen light off."
    assert d["enforced"] is False and d["ha"]["whisper"] is False and d["ha"]["phrase"] is True
    assert rec["whisper_text"] == result.text and rec["used"] == "whisper"
    assert TOKEN not in json.dumps(rec)


@pytest.mark.asyncio
async def test_decision_uses_speech_to_phrase_when_whisper_text_is_not_a_command(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, whisper_text="Turn the kitchen lid off.",
        phrase=_phrase_cls(text="turn the kitchen light off"),
    )
    d = _last_run(capsys)["decision"]
    assert d["use"] == "phrase" and d["text"] == "turn the kitchen light off"
    assert result.text == "Turn the kitchen lid off."


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [{"junk": True}, {"refuse": True}, {"slow": 3}, {"auth_bad": True}])
async def test_a_broken_home_assistant_changes_nothing_for_the_answer(tmp_path, token_file, capsys, mode):
    result, took, _ = await _run(tmp_path, token_file, ha_mode=mode, phrase=_phrase_cls(text="x"))
    rec = _last_run(capsys)
    assert result.text == "heard 16000" and took < 1.5
    assert rec["decision"]["use"] == "whisper_raw" and rec["decision"]["ha"]["available"] is False
    assert rec["error"] is None


@pytest.mark.asyncio
async def test_no_decision_without_configuration(tmp_path, capsys):
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    cfg = make_cfg(tmp_path, wport)
    proxy, pport = await start(partial(SttChainHandler, cfg, shared=Shared()))
    try:
        await run_command(pport)
    finally:
        await proxy.stop()
        await whisper.stop()
    assert "decision" not in _last_run(capsys)


# ---- configuration ----------------------------------------------------------------


def test_decision_settings_are_validated(monkeypatch, tmp_path):
    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    monkeypatch.setenv("DCV_DECISION", "maybe")
    with pytest.raises(ValueError, match="DCV_DECISION"):
        Config.from_env()
    monkeypatch.setenv("DCV_DECISION", "enforce")
    with pytest.raises(ValueError, match="DCV_HA_URL"):  # acting needs the check, same as shadow
        Config.from_env()
    monkeypatch.setenv("DCV_DECISION", "shadow")
    with pytest.raises(ValueError, match="DCV_HA_URL"):
        Config.from_env()
    monkeypatch.setenv("DCV_HA_URL", "http://ha:8123")
    monkeypatch.setenv("DCV_HA_TOKEN_FILE", str(tmp_path / "t"))
    with pytest.raises(ValueError, match="DCV_HA_URL"):
        Config.from_env()
    monkeypatch.setenv("DCV_HA_URL", "ws://ha:8123/api/websocket")
    cfg = Config.from_env()
    assert cfg.decision == "shadow" and cfg.ha_timeout == 1.5
    monkeypatch.setenv("DCV_DECISION", "off")
    assert Config.from_env().decision == "off"


@pytest.mark.asyncio
async def test_a_known_name_that_is_not_exposed_is_not_actionable(token_file):
    # Measured on a real HA: `match` is true for a light that is not exposed to voice, but
    # `targets` is empty and nothing would happen. That must not count as a command.
    ha = await FakeHa().start()
    checker = HaChecker(ha.url, token_file, 2.0)
    try:
        out = await checker.check(["turn on the hall light", "turn the kitchen light on"], "en")
    finally:
        await checker.close()
        await ha.stop()
    assert out[0] == {"match": True, "actionable": False, "intent": "HassTurnOn"}
    assert out[1]["actionable"] is True


def test_summary_handles_odd_shapes():
    for bad in (None, 5, [], {"intent": 3, "match": "yes", "slots": 4, "targets": []}):
        assert ha_check._summary(bad)["actionable"] is False
    assert ha_check._summary({"match": True, "intent": {"name": "x" * 100}})["intent"] is None


# ---- review fixes -----------------------------------------------------------------


def test_text_is_cleaned_and_a_whitespace_flood_cannot_stall_the_loop():
    from dcv_stt_chain.decide import MAX_TEXT_CHARS, clean

    assert clean("a   b \n c") == "a b c" and clean(None) == "" and clean(5) == ""
    t0 = time.monotonic()
    flood = clean("turn on" + " " * 30_000 + "x")
    sentences_to_check(flood, "")
    decide(flood, "", None)
    assert time.monotonic() - t0 < 0.5  # about 12 s before the pattern was made linear and the text cleaned first
    assert len(clean("x " * 50_000)) <= MAX_TEXT_CHARS


@pytest.mark.parametrize(
    "text",
    [
        "Don't turn off the kitchen light",
        "do not turn off the kitchen light.",
        "never turn on the kitchen light",
        "Turn on the kitchen light and turn off the fan",
        "turn on the kitchen light, then turn off the fan",
    ],
)
def test_a_negated_or_compound_command_is_never_cut_down_to_its_second_half(text):
    from dcv_stt_chain.decide import split

    parts = split(text)
    assert len(parts) >= 2  # it was cut at "turn"
    assert not any(ok for _, ok in parts[1:]), parts  # nothing after a negation or a conjunction is promoted


def test_run_together_commands_and_clean_sentences_are_still_picked_out():
    from dcv_stt_chain.decide import split

    assert split("turn the kitchen light on turn the kitchen light") == [
        ("turn the kitchen light on", True),
        ("turn the kitchen light", True),
    ]
    parts = split("Really? Turn the kitchen light off. There's something out there.")
    assert ("Turn the kitchen light off.", True) in parts


def test_texts_too_long_to_check_are_not_asked_about_and_the_cap_holds():
    long_chatter = "I was just saying. " * 40 + "Turn the kitchen light off."
    asked = sentences_to_check(long_chatter, "turn the Kitchen Light off")
    assert long_chatter not in asked and "turn the Kitchen Light off" in asked
    assert all(len(s) <= 512 for s in asked) and len(asked) <= 8
    many = " ".join(f"Sentence number {i}." for i in range(30))
    assert len(sentences_to_check(many, "phrase text")) <= 8
    assert sentences_to_check(many, "phrase text")[-1] == "phrase text"


def test_domain_only_sentences_need_a_target_too():
    base = {"match": True, "intent": {"name": "HassTurnOff"}, "slots": {"domain": "light"}}
    assert ha_check._summary({**base, "targets": {}})["actionable"] is False
    assert ha_check._summary({**base, "targets": {"light.a": {}}})["actionable"] is True


@pytest.mark.asyncio
async def test_an_idle_connection_dropped_by_home_assistant_is_reopened_without_a_pause(token_file):
    ha = await FakeHa(drop_after=1).start()
    checker = HaChecker(ha.url, token_file, 2.0)
    try:
        first = await checker.check(["turn the kitchen light off"], "en")
        second = await checker.check(["turn the kitchen light off"], "en")  # the socket was closed under us
    finally:
        await checker.close()
        await ha.stop()
    assert first and second and second[0]["actionable"] is True
    assert ha.connections == 2 and checker._down_until == 0.0


@pytest.mark.asyncio
async def test_a_refused_request_does_not_pause_or_reconnect(token_file):
    ha = await FakeHa(refuse=True).start()
    checker = HaChecker(ha.url, token_file, 2.0)
    try:
        assert await checker.check(["x"], "zz") is None
        assert await checker.check(["x"], "zz") is None
    finally:
        await checker.close()
        await ha.stop()
    assert ha.connections == 1 and checker._down_until == 0.0  # healthy connection kept, no backoff


@pytest.mark.asyncio
async def test_many_simultaneous_checks_share_one_connection_and_get_their_own_answers(token_file):
    ha = await FakeHa(slow=0.02).start()
    checker = HaChecker(ha.url, token_file, 10.0)
    try:
        sentences = [["turn the kitchen light off"] if i % 2 else ["nonsense"] for i in range(20)]
        results = await asyncio.gather(*(checker.check(s, "en") for s in sentences))
    finally:
        await checker.close()
        await ha.stop()
    assert ha.connections == 1
    for i, r in enumerate(results):
        assert r[0]["actionable"] is (i % 2 == 1)


@pytest.mark.asyncio
async def test_one_outage_pauses_once_however_many_runs_hit_it(token_file):
    ha = await FakeHa(slow=3).start()
    checker = HaChecker(ha.url, token_file, 0.4)
    try:
        results = await asyncio.gather(*(checker.check(["x"], "en") for _ in range(6)))
    finally:
        await checker.close()
        await ha.stop()
    assert all(r is None for r in results)
    assert checker._backoff == 10.0  # doubled once (5 -> 10), not once per run


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed_with_the_token(token_file):
    from http import HTTPStatus

    target = await FakeHa().start()

    def redirect(connection, request):
        response = connection.respond(HTTPStatus.FOUND, "moved\n")
        response.headers["Location"] = target.url
        return response

    async def unused(ws):
        pass

    server = await serve(unused, "127.0.0.1", 0, process_request=redirect)
    port = server.sockets[0].getsockname()[1]
    checker = HaChecker(f"ws://127.0.0.1:{port}/api/websocket", token_file, 2.0)
    try:
        assert await checker.check(["x"], "en") is None
    finally:
        await checker.close()
        server.close()
        await server.wait_closed()
        await target.stop()
    assert target.connections == 0 and target.tokens == []  # the token never went to the redirect target


@pytest.mark.asyncio
async def test_a_replaced_token_file_is_picked_up_after_a_rejection(tmp_path):
    f = tmp_path / "tok"
    f.write_text("wrong-token")
    ha = await FakeHa().start()
    checker = HaChecker(ha.url, str(f), 2.0, token="wrong-token")
    try:
        assert await checker.check(["x"], "en") is None
        f.write_text(TOKEN)
        checker._down_until = 0.0  # as if the five minutes were over
        out = await checker.check(["turn the kitchen light off"], "en")
    finally:
        await checker.close()
        await ha.stop()
    assert out and out[0]["actionable"] is True


def test_token_file_edge_cases(tmp_path):
    f = tmp_path / "t"
    f.write_bytes(b"\xef\xbb\xbfabc")  # a byte-order mark
    with pytest.raises(ValueError, match="ASCII"):
        ha_check.read_token(str(f))
    f.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError) as err:
        ha_check.read_token(str(f))
    assert "0xff" not in str(err.value) and "position" not in str(err.value)  # no byte or offset leaked
    f.write_bytes(b"A" * 10_000_000)
    with pytest.raises(ValueError):
        ha_check.read_token(str(f))  # refused after reading a few KB, not the whole file


def test_ha_settings_are_bounded_and_do_not_echo_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("DCV_WHISPER_URI", "tcp://x:1")
    monkeypatch.setenv("DCV_DECISION", "shadow")
    monkeypatch.setenv("DCV_HA_TOKEN_FILE", str(tmp_path / "t"))
    monkeypatch.setenv("DCV_HA_URL", "ws://ha:8123/api/websocket")
    for bad in ("0", "11", "60", "nan"):
        monkeypatch.setenv("DCV_HA_TIMEOUT", bad)
        with pytest.raises(ValueError):
            Config.from_env()
    monkeypatch.setenv("DCV_HA_TIMEOUT", "10")
    assert Config.from_env().ha_timeout == 10.0
    for bad in (
        "ws://user:hunter2@ha:8123/api/websocket",
        "ws://ha:8123/api/websocket?token=hunter2",
        "ws://ha:8123/x#hunter2",
    ):
        monkeypatch.setenv("DCV_HA_URL", bad)
        with pytest.raises(ValueError) as err:
            Config.from_env()
        assert "hunter2" not in str(err.value)


@pytest.mark.asyncio
async def test_a_run_queued_behind_a_failing_call_does_not_reconnect_during_the_pause(token_file):
    ha = await FakeHa(slow=0.7).start()
    checker = HaChecker(ha.url, token_file, 0.5)
    try:
        first = asyncio.ensure_future(checker.check(["x"], "en"))  # times out at 0.5 s
        await asyncio.sleep(0.3)
        second = asyncio.ensure_future(checker.check(["x"], "en"))  # already waiting for the lock when it fails
        assert await first is None and await second is None
    finally:
        await checker.close()
        await ha.stop()
    assert ha.connections == 1  # the second run did not open a new connection into the outage
    assert checker._backoff == 10.0  # and the pause was lengthened once


@pytest.mark.asyncio
async def test_a_home_assistant_that_freezes_after_login_starts_the_pause_within_the_callers_limit(token_file):
    # The peer logs in, then stops reading: it never completes a closing handshake. A polite close
    # would outlast the proxy's own outer limit (the check's timeout + 0.5 s) and the pause would
    # never start, so every run would retry and hold its connection.
    async def frozen(ws):
        await ws.send(json.dumps({"type": "auth_required"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "auth_ok"}))
        ws.transport.pause_reading()
        await asyncio.sleep(30)

    server = await serve(frozen, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    checker = HaChecker(f"ws://127.0.0.1:{port}/api/websocket", token_file, 1.0)
    try:
        t0 = time.monotonic()
        out = await asyncio.wait_for(checker.check(["x"], "en"), 1.5)  # the outer limit in handler._decide
        took = time.monotonic() - t0
        assert out is None and took < 1.4
        assert checker._down_until > time.monotonic()  # the pause did start
        t1 = time.monotonic()
        assert await checker.check(["x"], "en") is None
        assert time.monotonic() - t1 < 0.1  # and the next run is not held at all
    finally:
        await checker.close()
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), 2.0)
        except asyncio.TimeoutError:
            pass


def test_a_run_of_turn_words_is_cheap_to_split(monkeypatch):
    from dcv_stt_chain import decide as decide_mod

    class Spy:
        def __init__(self, real):
            self.real, self.calls, self.longest = real, 0, 0

        def search(self, text):
            self.calls += 1
            self.longest = max(self.longest, len(text))
            return self.real.search(text)

    negation, conjunction = Spy(decide_mod._NEGATION), Spy(decide_mod._CONJUNCTION)
    monkeypatch.setattr(decide_mod, "_NEGATION", negation)
    monkeypatch.setattr(decide_mod, "_CONJUNCTION", conjunction)
    parts = decide_mod.split(decide_mod.clean("turn " * 400))
    assert len(parts) == 6
    # Work is bounded by the piece limit and by the 200-character window, not by the input:
    # about 400 searches over strings of up to 2000 characters before it stopped at the limit.
    assert negation.calls <= 6 and conjunction.calls <= 6
    assert negation.longest <= 200 and conjunction.longest <= 200


@pytest.mark.asyncio
async def test_cancelling_a_check_mid_call_starts_the_pause_but_cancelling_a_waiter_does_not(token_file):
    ha = await FakeHa(slow=2).start()
    try:
        # 1. cancelled while talking to Home Assistant (as the proxy's outer limit would): pause starts
        checker = HaChecker(ha.url, token_file, 5.0)
        task = asyncio.ensure_future(checker.check(["x"], "en"))
        await asyncio.sleep(0.4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert checker._down_until > time.monotonic()
        await checker.close()

        # 2. cancelled while only waiting behind another call for the lock: no pause
        checker = HaChecker(ha.url, token_file, 5.0)
        holder = asyncio.ensure_future(checker.check(["x"], "en"))
        await asyncio.sleep(0.4)
        waiter = asyncio.ensure_future(checker.check(["y"], "en"))
        await asyncio.sleep(0.2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert checker._down_until == 0.0
        holder.cancel()
        with pytest.raises(asyncio.CancelledError):
            await holder
        await checker.close()
    finally:
        await ha.stop()


# ---- enforcing: the fast pass and the fallback --------------------------------------


@pytest.mark.asyncio
async def test_enforce_fast_pass_answers_without_waiting_for_speech_to_phrase(tmp_path, token_file, capsys):
    hang = _phrase_cls(hang=True)
    result, took, ha = await _run(
        tmp_path, token_file, phrase=hang, whisper_text="Turn  the kitchen light off.", decision="enforce"
    )
    rec = _last_run(capsys)
    assert result.text == "Turn  the kitchen light off."  # Whisper's exact words, not the cleaned copy
    assert took < 1.0  # phrase_timeout is 3 s: it was not waited for
    d = rec["decision"]
    assert d["use"] == "whisper" and d["enforced"] is True
    assert rec["phrase_error"] == "not_needed"
    assert all("turn on the light" not in (x or []) for x in ha.sentences_seen)  # never asked about it


@pytest.mark.asyncio
async def test_enforce_sends_the_audio_to_both_engines_while_it_is_still_arriving(tmp_path, token_file, capsys):
    from wyoming.asr import Transcribe
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.client import AsyncClient

    class W(FakeWhisper):
        got = 0

        async def handle_event(self, event):
            if AudioChunk.is_type(event.type):
                type(self).got += len(event.payload or b"")
            return await super().handle_event(event)

    P = _phrase_cls(text="turn the kitchen light off")
    ha = await FakeHa().start()
    whisper, wport = await start(lambda r, w: W(r, w))
    pserver, pport = await start(lambda r, w: P(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}",
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{port}")
    try:
        await client.connect()
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=b"\x01\x00" * 1600).event())
        await asyncio.sleep(0.5)  # the command is still being spoken: no AudioStop yet
        assert W.got == 3200 and P.heard == 3200  # both engines already have the audio so far
        await client.write_event(AudioStop().event())
        await asyncio.wait_for(client.read_event(), 5)
    finally:
        await client.disconnect()
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()


@pytest.mark.asyncio
async def test_enforce_trimmed_command_is_answered_without_waiting_for_speech_to_phrase(tmp_path, token_file, capsys):
    result, took, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(hang=True),
        whisper_text="I hate this. Turn the kitchen light off. Why?", decision="enforce",
    )
    assert result.text == "Turn the kitchen light off." and took < 1.0
    assert _last_run(capsys)["decision"]["use"] == "whisper_trimmed"


@pytest.mark.asyncio
async def test_enforce_falls_to_speech_to_phrase_when_whisper_is_not_actionable(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(text="turn the kitchen light off"),
        whisper_text="Turn the kitchen lid off.", decision="enforce",
    )
    rec = _last_run(capsys)
    assert result.text == "turn the kitchen light off"
    assert rec["decision"]["use"] == "phrase" and rec["decision"]["enforced"] is True
    assert rec["whisper_text"] == "Turn the kitchen lid off."  # the log still has what Whisper said


@pytest.mark.asyncio
async def test_enforce_uses_speech_to_phrase_when_whisper_heard_nothing(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(text="turn the kitchen light off"),
        whisper_text="", decision="enforce",
    )
    assert result.text == "turn the kitchen light off"


@pytest.mark.asyncio
async def test_enforce_neither_actionable_answers_whispers_exact_text(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(text="blah blah"),
        whisper_text="Turn  the kitchen lid off.", decision="enforce",
    )
    assert result.text == "Turn  the kitchen lid off."
    assert _last_run(capsys)["decision"]["use"] == "whisper_raw"


@pytest.mark.asyncio
async def test_enforce_slow_speech_to_phrase_is_waited_for_only_up_to_its_own_limit(tmp_path, token_file, capsys):
    result, took, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(hang=True),
        whisper_text="Turn the kitchen lid off.", decision="enforce", phrase_timeout=0.5,
    )
    assert result.text == "Turn the kitchen lid off." and took < 2.0
    assert _last_run(capsys)["phrase_error"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [{"junk": True}, {"refuse": True}, {"slow": 3}, {"auth_bad": True}])
async def test_enforce_with_home_assistant_unreachable_answers_whisper_without_waiting(tmp_path, token_file, capsys, mode):
    result, took, _ = await _run(
        tmp_path, token_file, ha_mode=mode, phrase=_phrase_cls(hang=True),
        whisper_text="Turn the kitchen lid off.", decision="enforce",
    )
    assert result.text == "Turn the kitchen lid off." and took < 1.8  # not held for Speech-to-Phrase
    assert _last_run(capsys)["decision"]["ha"]["available"] is False


@pytest.mark.asyncio
async def test_enforce_without_speech_to_phrase_still_passes_a_good_command(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, whisper_text="Turn the kitchen light off.", decision="enforce"
    )
    assert result.text == "Turn the kitchen light off."


def test_negations_stop_a_command_being_cut_out_of_a_refusal():
    from dcv_stt_chain.decide import split

    for refusal in ("I didn't mean it", "that isn't right", "she doesn't want it", "we cant", "it wasnt"):
        parts = split(f"{refusal} turn off the kitchen light")
        assert parts[-1][0].lower().startswith("turn") and parts[-1][1] is False, refusal
    parts = split("okay turn off the kitchen light")
    assert parts[-1][1] is True  # nothing negative before it


# ---- review fixes for the gate --------------------------------------------------------


def test_speech_to_phrase_cannot_overturn_a_negated_command():
    ask = ["don't turn off the kitchen light", "turn off the kitchen light"]
    res = _results(ask, {"turn off the kitchen light"})
    out = decide("don't turn off the kitchen light", "turn off the kitchen light", res, ask)
    assert out["use"] == "whisper_raw"  # the swap would do the opposite of what was said
    # a negation Speech-to-Phrase also heard is no reason to refuse it
    ask = ["do not turn off the kitchen light", "do not turn off the kitchen light now"]
    res = _results(ask, {"do not turn off the kitchen light now"})
    assert decide(ask[0], ask[1], res, ask)["use"] == "phrase"


@pytest.mark.asyncio
async def test_enforce_logs_what_was_sent_and_which_engine_it_came_from(tmp_path, token_file, capsys):
    result, _, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(text="turn the kitchen light off"),
        whisper_text="", decision="enforce",
    )
    rec = _last_run(capsys)
    assert result.text == "turn the kitchen light off"
    assert rec["used"] == "phrase" and rec["answer"] == "turn the kitchen light off"
    assert rec["whisper_text"] == ""


@pytest.mark.asyncio
async def test_enforce_keeps_speech_to_phrases_text_in_the_log_when_it_answered_first(tmp_path, token_file, capsys):
    class SlowWhisper(FakeWhisper):
        async def handle_event(self, event):
            from wyoming.asr import Transcript
            from wyoming.audio import AudioStop

            if AudioStop.is_type(event.type):
                await asyncio.sleep(0.4)  # Speech-to-Phrase is done long before this
                await self.write_event(Transcript(text="Turn the kitchen light off.").event())
                return True
            return await super().handle_event(event)

    ha = await FakeHa().start()
    whisper, wport = await start(lambda r, w: SlowWhisper(r, w))
    pserver, pport = await start(lambda r, w: _phrase_cls(text="blah")(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}",
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        result = await run_command(port)
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    rec = _last_run(capsys)
    assert result.text == "Turn the kitchen light off."
    assert rec["phrase_text"] == "blah" and rec["phrase_error"] is None  # kept for the replay


@pytest.mark.asyncio
async def test_enforce_does_not_wait_for_a_shadow_that_was_given_up_on(tmp_path, token_file, capsys, monkeypatch):
    from dcv_stt_chain import handler as handler_mod

    monkeypatch.setattr(handler_mod, "_SHADOW_MAX_QUEUED", 1)  # the queue overflows at once
    result, took, _ = await _run(
        tmp_path, token_file, phrase=_phrase_cls(hang=True),
        whisper_text="Turn the kitchen lid off.", decision="enforce",
    )
    assert result.text == "Turn the kitchen lid off." and took < 1.5  # phrase_timeout is 3 s
    assert _last_run(capsys)["phrase_error"] == "dropped"


@pytest.mark.asyncio
async def test_enforce_stops_waiting_for_a_speech_to_phrase_that_just_timed_out(tmp_path, token_file, capsys):
    ha = await FakeHa().start()
    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))

    class W(FakeWhisper):
        async def handle_event(self, event):
            from wyoming.asr import Transcript
            from wyoming.audio import AudioStop

            if AudioStop.is_type(event.type):
                await self.write_event(Transcript(text="Turn the kitchen lid off.").event())
                return True
            return await super().handle_event(event)

    await whisper.stop()
    whisper, wport = await start(lambda r, w: W(r, w))
    pserver, pport = await start(lambda r, w: _phrase_cls(hang=True)(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}", phrase_timeout=0.6,
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        t0 = time.monotonic()
        await run_command(port)
        first = time.monotonic() - t0
        rec1 = _last_run(capsys)
        t0 = time.monotonic()
        await run_command(port)
        second = time.monotonic() - t0
        rec2 = _last_run(capsys)
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    assert rec1["phrase_error"] == "timeout" and first > 0.5
    assert rec2["phrase_error"] == "skipped_slow" and second < first  # not held again


@pytest.mark.asyncio
async def test_enforce_second_home_assistant_check_failing_answers_whispers_text(tmp_path, token_file, capsys):
    # The first check (Whisper's text) works and finds nothing; the second (Speech-to-Phrase's) gets junk.
    result, _, _ = await _run(
        tmp_path, token_file, ha_mode={"junk_after": 1}, phrase=_phrase_cls(text="turn the kitchen light off"),
        whisper_text="Turn the kitchen lid off.", decision="enforce",
    )
    assert result.text == "Turn the kitchen lid off."
    assert _last_run(capsys)["decision"]["use"] == "whisper_raw"


@pytest.mark.asyncio
async def test_enforce_whisper_error_with_speech_to_phrase_answering(tmp_path, token_file, capsys):
    ha = await FakeHa().start()
    port = free_port()  # nothing listens: Whisper is down
    pserver, pport = await start(lambda r, w: _phrase_cls(text="turn the kitchen light off")(r, w))
    cfg = make_cfg(
        tmp_path, port, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}",
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, pp = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        result = await run_command(pp)
    finally:
        await proxy.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    rec = _last_run(capsys)
    assert result.text == "turn the kitchen light off"  # Whisper down: Speech-to-Phrase carries it
    assert rec["error"] and rec["decision"]["use"] == "phrase"


@pytest.mark.asyncio
async def test_enforce_two_rooms_at_once_get_their_own_answers(tmp_path, token_file, capsys):
    ha = await FakeHa(slow=0.3).start()
    texts = iter(["Turn the kitchen light off.", "Turn the kitchen lid off."])

    class W(FakeWhisper):
        async def handle_event(self, event):
            from wyoming.asr import Transcript
            from wyoming.audio import AudioStop

            if AudioStop.is_type(event.type):
                await self.write_event(Transcript(text=next(texts)).event())
                return True
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: W(r, w))
    pserver, pport = await start(lambda r, w: _phrase_cls(text="turn the kitchen light on")(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=2.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}",
    )
    checker = HaChecker(ha.url, token_file, 2.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        a, b = await asyncio.gather(run_command(port), run_command(port))
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    assert sorted([a.text, b.text]) == sorted(["Turn the kitchen light off.", "turn the kitchen light on"])


@pytest.mark.asyncio
async def test_enforce_skips_a_check_it_has_no_time_for_instead_of_pausing_every_room(tmp_path, token_file, capsys):
    from wyoming.asr import Transcript
    from wyoming.audio import AudioStop

    class SlowWhisper(FakeWhisper):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                await asyncio.sleep(2.5)  # a busy GPU: the gate's budget is down to its floor
                await self.write_event(Transcript(text="Turn the kitchen lid off.").event())
                return True
            return await super().handle_event(event)

    ha = await FakeHa(slow=0.8).start()
    whisper, wport = await start(lambda r, w: SlowWhisper(r, w))
    pserver, pport = await start(lambda r, w: _phrase_cls(text="turn the kitchen light off")(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}", phrase_timeout=0.2, result_timeout=5.0,
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        result = await run_command(port)
        paused = checker._down_until
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    rec = _last_run(capsys)
    assert result.text == "Turn the kitchen lid off."  # no time left to check Speech-to-Phrase's text
    assert paused == 0.0  # and the checks for the other rooms were not paused
    assert rec["decision"]["ha"]["available"] is True and rec["decision"]["use"] == "whisper_raw"


def test_stop_is_a_command_not_a_reversal_when_deciding_whether_a_swap_overturns():
    from dcv_stt_chain.decide import overturns

    assert not overturns("stop the musik", "pause the music")
    assert overturns("don't pause the music", "pause the music")
    assert overturns("never mind turn it off", "turn it off")


@pytest.mark.asyncio
async def test_a_client_leaving_mid_command_still_ends_speech_to_phrases_audio(tmp_path):
    from wyoming.asr import Transcribe
    from wyoming.audio import AudioChunk, AudioStart, AudioStop
    from wyoming.client import AsyncClient

    stops = []

    class P(FakePhrase):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                stops.append(1)
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    pserver, pport = await start(lambda r, w: P(r, w))
    cfg = make_cfg(tmp_path, wport, mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}")
    proxy, port = await start(partial(SttChainHandler, cfg))
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{port}")
    try:
        await client.connect()
        await client.write_event(Transcribe(language="en").event())
        await client.write_event(AudioStart(rate=16000, width=2, channels=1).event())
        await client.write_event(AudioChunk(rate=16000, width=2, channels=1, audio=bytes(3200)).event())
        await asyncio.sleep(0.3)
        await client.disconnect()  # no AudioStop: the command is abandoned
        await asyncio.sleep(0.8)
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
    assert stops == [1]  # Speech-to-Phrase was told the audio ended, so it can finish and free its decoder


@pytest.mark.asyncio
async def test_a_polite_close_that_never_finishes_is_aborted_not_left_open():
    from dcv_stt_chain.handler import close_client

    class Transport:
        aborted = False

        def abort(self):
            self.aborted = True

    class Writer:
        transport = Transport()

    class Stuck:
        _writer = Writer()

        async def disconnect(self):
            await asyncio.sleep(30)  # a peer that is not reading: the polite close never completes

    client = Stuck()
    t0 = time.monotonic()
    await close_client(client, abort=False)
    assert client._writer.transport.aborted and time.monotonic() - t0 < 2.0


@pytest.mark.asyncio
async def test_leaving_before_audio_start_does_not_send_speech_to_phrase_a_lone_stop(tmp_path):
    from wyoming.asr import Transcribe
    from wyoming.audio import AudioStop
    from wyoming.client import AsyncClient

    stops = []

    class P(FakePhrase):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                stops.append(1)
            return await super().handle_event(event)

    whisper, wport = await start(lambda r, w: FakeWhisper(r, w))
    pserver, pport = await start(lambda r, w: P(r, w))
    cfg = make_cfg(tmp_path, wport, mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}")
    proxy, port = await start(partial(SttChainHandler, cfg))
    client = AsyncClient.from_uri(f"tcp://127.0.0.1:{port}")
    try:
        await client.connect()
        await client.write_event(Transcribe(language="en").event())
        await asyncio.sleep(0.3)
        await client.disconnect()  # Transcribe only: no AudioStart was ever sent
        await asyncio.sleep(0.8)
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
    assert stops == []


@pytest.mark.asyncio
async def test_enforce_marks_a_speech_to_phrase_text_it_never_checked(tmp_path, token_file, capsys):
    # same set-up as the no-time-for-a-check test: the second check is skipped, and the log says so
    from wyoming.asr import Transcript
    from wyoming.audio import AudioStop

    class SlowWhisper(FakeWhisper):
        async def handle_event(self, event):
            if AudioStop.is_type(event.type):
                await asyncio.sleep(2.5)
                await self.write_event(Transcript(text="Turn the kitchen lid off.").event())
                return True
            return await super().handle_event(event)

    ha = await FakeHa(slow=0.8).start()
    whisper, wport = await start(lambda r, w: SlowWhisper(r, w))
    pserver, pport = await start(lambda r, w: _phrase_cls(text="turn the kitchen light off")(r, w))
    cfg = make_cfg(
        tmp_path, wport, decision="enforce", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0,
        mode="compare", phrase_uri=f"tcp://127.0.0.1:{pport}", phrase_timeout=0.2, result_timeout=5.0,
    )
    checker = HaChecker(ha.url, token_file, 1.0)
    proxy, port = await start(partial(SttChainHandler, cfg, shared=Shared(ha=checker)))
    try:
        await run_command(port)
    finally:
        await proxy.stop()
        await whisper.stop()
        await pserver.stop()
        await checker.close()
        await ha.stop()
    rec = _last_run(capsys)
    assert rec["phrase_text"] == "turn the kitchen light off"
    assert rec["decision"]["ha"]["phrase_checked"] is False


def test_cannot_and_shant_count_as_reversals():
    from dcv_stt_chain.decide import overturns

    assert overturns("you cannot turn it off", "turn it off")
    assert overturns("you shan't turn it off", "turn it off")
    assert overturns("he mightn't turn it off", "turn it off")


def test_a_command_is_not_cut_out_after_cannot_shant_or_mightnt():
    from dcv_stt_chain.decide import split

    for refusal in ("you cannot", "she shan't", "he mightn't"):
        parts = split(f"{refusal} turn off the kitchen light")
        assert parts[-1][0].lower().startswith("turn") and parts[-1][1] is False, refusal
