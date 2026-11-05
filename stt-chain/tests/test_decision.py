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
from test_passthrough import FakeWhisper, make_cfg, run_command, start
from test_robustness import _last_run, _phrase_cls

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
        tmp_path, wport, decision="shadow", ha_url=ha.url, ha_token_file=token_file, ha_timeout=1.0, **extra, **over
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
    monkeypatch.setenv("DCV_DECISION", "enforce")
    with pytest.raises(ValueError, match="DCV_DECISION"):
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
