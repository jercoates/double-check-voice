"""Replay saved command clips through any number of Wyoming STT engines and compare.

    python replay.py CLIPS... --engine NAME=URI[@CLEANUP] [--engine ...]
                     [--expected expected.csv] [--out results.jsonl]

    --engine whisper=tcp://host:10300
    --engine whisper_light=tcp://host:10300@light     same Whisper, audio cleaned first
    --engine phrase=tcp://host:10301

CLEANUP is none (default), light or boost (see dcv_stt_chain/cleanup.py). The old
--whisper URI and --phrase URI still work and mean --engine whisper=URI / phrase=URI.

CLIPS are WAV files or folders (searched recursively for *.wav), as saved by
dcv-stt-chain: 16 kHz, 16-bit, mono. Each clip is sent to every engine the way Home
Assistant sends audio (Transcribe, AudioStart, AudioChunk..., AudioStop), engines in
parallel, clips one after another. Nothing is written except --out. Needs the wyoming
package (pip install wyoming==1.10.2).

expected.csv (optional): two columns, clip file name and what was really said, e.g.
    043926_lite-room2_bf30eeb6.wav,turn on the kitchen light
It adds an exact-match flag and a word error rate per engine.
"""

import argparse
import asyncio
import csv
import json
import re
import sys
import time
import wave
from pathlib import Path
from typing import Optional

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.error import Error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "stt-chain"))
from dcv_stt_chain import cleanup  # noqa: E402

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_FRAMES = 1024
MAX_ENGINES = 8


def norm(text: str) -> str:
    """Lower-case words only, so punctuation and case don't count as differences."""
    return " ".join(re.findall(r"[\w']+", (text or "").lower()))


def wer(expected: str, heard: str) -> float:
    """Word error rate: word-level edit distance over the expected word count."""
    ref, hyp = norm(expected).split(), norm(heard).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1] / len(ref)


def parse_engine(spec: str) -> tuple:
    """'name=tcp://host:port[@preset]' -> (name, uri, preset)."""
    name, sep, rest = spec.partition("=")
    if not sep or not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name) or "://" not in rest:
        raise ValueError(f"bad --engine {spec!r}: use NAME=tcp://host:port[@cleanup]")
    uri, _, preset = rest.rpartition("@") if "@" in rest.split("://", 1)[1] else (rest, "", "none")
    if preset not in cleanup.PRESETS:
        raise ValueError(f"bad cleanup {preset!r} in {spec!r}: use {', '.join(cleanup.PRESETS)}")
    return name, uri, preset


def find_clips(paths: list) -> list:
    clips = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            clips.extend(sorted(p.rglob("*.wav")))
        elif p.is_file():
            clips.append(p)
        else:
            print(f"skipping {raw}: not found", file=sys.stderr)
    return clips


def read_pcm(path: Path) -> Optional[bytes]:
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (RATE, WIDTH, CHANNELS):
                print(f"skipping {path.name}: not 16 kHz 16-bit mono", file=sys.stderr)
                return None
            return w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError) as err:
        print(f"skipping {path.name}: {type(err).__name__}", file=sys.stderr)
        return None


async def ask(uri: str, pcm: bytes, language: str, timeout: float) -> dict:
    """Send one clip to one engine. Returns {text, ms, error}. Never raises."""
    client = AsyncClient.from_uri(uri)
    loop = asyncio.get_running_loop()
    out = {"text": "", "ms": None, "error": None}
    try:
        await asyncio.wait_for(client.connect(), timeout)

        async def send(event):  # an engine that stops reading must not hang the replay
            await asyncio.wait_for(client.write_event(event), timeout)

        await send(Transcribe(language=language).event())
        await send(AudioStart(rate=RATE, width=WIDTH, channels=CHANNELS).event())
        step = CHUNK_FRAMES * WIDTH * CHANNELS
        for i in range(0, len(pcm), step):
            await send(
                AudioChunk(rate=RATE, width=WIDTH, channels=CHANNELS, audio=pcm[i : i + step]).event()
            )
        await send(AudioStop().event())
        t_stop = loop.time()
        deadline = t_stop + timeout
        while True:
            event = await asyncio.wait_for(client.read_event(), max(0.1, deadline - loop.time()))
            if event is None:
                out["error"] = "closed"
                break
            if Error.is_type(event.type):
                out["error"] = "error"
                break
            if Transcript.is_type(event.type):
                out["text"] = Transcript.from_event(event).text or ""
                out["ms"] = round((loop.time() - t_stop) * 1000)
                break
    except asyncio.TimeoutError:
        out["error"] = "timeout"
    except Exception as err:  # noqa: BLE001
        out["error"] = type(err).__name__
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), 1.0)
        except Exception:  # noqa: BLE001
            pass
    return out


def load_expected(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:  # -sig: Excel adds a BOM
        return {row[0].strip(): row[1].strip() for row in csv.reader(f) if len(row) >= 2}


def engines_from_args(args) -> list:
    specs = list(args.engine or [])
    if args.whisper:
        specs.insert(0, f"whisper={args.whisper}")
    if args.phrase:
        specs.append(f"phrase={args.phrase}")
    engines = [parse_engine(s) for s in specs]
    names = [e[0] for e in engines]
    if not engines or len(set(names)) != len(names) or len(engines) > MAX_ENGINES:
        raise ValueError(f"give 1 to {MAX_ENGINES} engines with different names")
    return engines


async def run(args) -> list:
    engines = engines_from_args(args)
    expected = load_expected(args.expected)
    rows = []
    for clip in find_clips(args.clips):
        pcm = read_pcm(clip)
        if pcm is None:
            continue
        cleaned = {p: cleanup.apply(pcm, p) for p in {e[2] for e in engines}}
        results = await asyncio.gather(
            *(ask(uri, cleaned[preset], args.language, args.timeout) for _, uri, preset in engines)
        )
        row = {
            "clip": clip.name,
            "seconds": round(len(pcm) / (RATE * WIDTH * CHANNELS), 2),
            "engines": {name: res for (name, _, _), res in zip(engines, results)},
        }
        first = results[0]["text"]
        row["agree_with_first"] = {
            name: bool(norm(res["text"]) and norm(res["text"]) == norm(first))
            for (name, _, _), res in zip(engines[1:], results[1:])
        }
        if clip.name in expected:
            exp = expected[clip.name]
            row["expected"] = exp
            for (name, _, _), res in zip(engines, results):
                row[f"{name}_exact"] = norm(res["text"]) == norm(exp)
                row[f"{name}_wer"] = round(wer(exp, res["text"]), 3)
        rows.append(row)
        print(
            f"{clip.name}: "
            + " | ".join(f"{n}={r['text']!r} ({r['ms']} ms{', ' + r['error'] if r['error'] else ''})"
                         for (n, _, _), r in zip(engines, results))
        )
    return rows


def summarize(rows: list) -> dict:
    names = list(rows[0]["engines"]) if rows else []
    s = {"clips": len(rows)}
    for name in names:
        res = [r["engines"][name] for r in rows]
        ms = [x["ms"] for x in res if x["ms"] is not None]
        s[name] = {
            "nonempty": sum(bool(x["text"]) for x in res),
            "errors": sum(bool(x["error"]) for x in res),
            "avg_ms": round(sum(ms) / len(ms)) if ms else None,
            "agree_with_first": sum(bool(r["agree_with_first"].get(name)) for r in rows),
        }
        scored = [r for r in rows if f"{name}_wer" in r]
        if scored:
            s[name]["exact"] = f"{sum(r[f'{name}_exact'] for r in scored)}/{len(scored)}"
            s[name]["mean_wer"] = round(sum(r[f"{name}_wer"] for r in scored) / len(scored), 3)
    return s


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("clips", nargs="+", help="WAV files or folders")
    ap.add_argument("--engine", action="append", help="NAME=tcp://host:port[@cleanup], repeatable")
    ap.add_argument("--whisper", default="", help="same as --engine whisper=URI")
    ap.add_argument("--phrase", default="", help="same as --engine phrase=URI")
    ap.add_argument("--language", default="en")
    ap.add_argument("--timeout", type=float, default=15.0, help="seconds per engine per clip")
    ap.add_argument("--expected", help="CSV: clip file name, what was really said")
    ap.add_argument("--out", default="replay-results.jsonl")
    args = ap.parse_args(argv)
    try:
        engines = engines_from_args(args)
    except ValueError as err:
        ap.error(str(err))
    rows = asyncio.run(run(args))
    summary = summarize(rows)
    meta = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "engines": {n: {"uri": u, "cleanup": p} for n, u, p in engines},
        "language": args.language,
        "timeout": args.timeout,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(json.dumps({"settings": meta, "summary": summary}) + "\n")
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
