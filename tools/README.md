# tools

## replay.py

Replays saved command clips through any number of Wyoming speech-to-text engines and compares
what each one heard. Use it on the clips `dcv-stt-chain` saves, to test a change or a different
engine against the same audio.

```
pip install -r ../requirements-dev.txt
python replay.py CLIPS... --engine whisper=tcp://host:10300 --engine phrase=tcp://host:10301 \
    [--expected expected.csv] [--out results.jsonl]
```

- `CLIPS` are WAV files or folders (16 kHz, 16-bit, mono, as the proxy saves them).
- `--engine NAME=URI[@CLEANUP]` can be repeated (up to 8). `CLEANUP` is `none` (default), `light` or
  `boost` (see `stt-chain/dcv_stt_chain/cleanup.py`) and is applied to the audio only for that engine.
- `--expected` is a CSV of clip file name and what was really said; it adds an exact-match flag and a
  word error rate per engine.
- Every send and read has a timeout (`--timeout`, default 15 s); a dead engine is reported, not fatal.
- Nothing is written except `--out`. Clips contain your household's voices: keep them out of git.

Run the tests with `pytest` in this folder.
