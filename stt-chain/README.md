# dcv-stt-chain

Wyoming speech-to-text proxy. Home Assistant sees it as one more STT engine.

**Version 0.1 is pass-through only:** it forwards each command to faster-whisper,
returns Whisper's transcript, and logs every run. This produces the baseline. With
`DCV_MODE=compare` it also asks Speech-to-Phrase and logs both answers, still
returning only Whisper's. `fallback` and `phrase_only` come after the logs justify them.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DCV_WHISPER_URI` | required | Whisper's Wyoming address, e.g. `tcp://192.168.1.10:10300` |
| `DCV_LISTEN_URI` | `tcp://0.0.0.0:10310` | Where this proxy listens (inside the container). Wyoming has no auth: only the HA-facing network may reach this port. The proxy logs a warning when it listens on all interfaces |
| `DCV_DEVICE` | `default` | Name logged with each run and put in audio file names (letters, digits, `_ . -`). One proxy port per profile: run one container per device name |
| `DCV_AUDIO_DIR` | `/data/audio` | Where raw command audio is saved as WAV (files are mode 0640) |
| `DCV_AUDIO_RETENTION_DAYS` | `30` | WAV files older than this are deleted (checked hourly). `0` keeps by age forever |
| `DCV_AUDIO_MAX_MB` | `2048` | Total size cap for saved audio. When full, the oldest files are deleted to make room, so the newest commands are always kept. Because it evicts oldest-first, a flood of junk audio from the LAN would push real clips out: that is why the port must stay LAN-only. `0` = no cap |
| `DCV_CONNECT_TIMEOUT` | `2.0` s | Time allowed to connect to Whisper (and to Speech-to-Phrase), and for each short write: to Whisper, to Home Assistant for its answer, and the describe round trip |
| `DCV_RESULT_TIMEOUT` | `10.0` s | One deadline, from the end of the audio, for Whisper's answer. Starting value; tune from the baseline logs |
| `DCV_IDLE_TIMEOUT` | `30.0` s | A client silent this long is disconnected |
| `DCV_MAX_CAPTURE_S` | `30.0` s | Longest command accepted. Audio past this is dropped (not saved, not sent to Whisper) and the run is logged as `audio_truncated` |
| `DCV_MAX_CONNECTIONS` | `16` | Extra connections beyond this are refused |
| `DCV_ALLOWED_PEERS` | required (see next row) | Comma-separated client IP addresses allowed to connect: set it to Home Assistant's IP. Plain IPs only (no ranges, ports or names; a typo stops the container at startup). **The proxy refuses to start with this empty** unless `DCV_ALLOW_ANY_PEER=1` is set. Loopback and the proxy's own listen address are always allowed (that is how the health check connects) |
| `DCV_ALLOW_ANY_PEER` | `0` | Set to `1` to accept connections from any host that can reach the port. Wyoming has no authentication, so use this only on a network you fully control. The proxy logs a warning at startup when it is on |
| `DCV_MAX_PER_IP` | `8` | Same, per client IP. Home Assistant is one IP for every satellite, so keep this above the number of rooms that can talk at once |
| `DCV_MAX_CONNECTION_S` | `120.0` s | A connection is closed after this long, however busy. Home Assistant opens one per command, so this only stops a client holding a slot |
| `DCV_RUN_LOG_FILE` | `1` | Also write each run's log line to `<audio folder>/<day>/runs_<device>.jsonl`, next to that day's clips (mode 0640, removed with the audio by `DCV_AUDIO_RETENTION_DAYS`). It contains transcripts of what was said in the house, so it is in any backup or share of the appdata folder. Runs with no audio are not written; each file stops growing at 20 MB (stdout still logs). The run-log files are NOT counted in `DCV_AUDIO_MAX_MB` (only age removes them), so budget up to 20 MB per device per day on top of it. Temporary data gathering: `0` turns it off. If the write fails the run still completes. Log lines are ASCII-only (non-ASCII is escaped)
| `DCV_MODE` | `passthrough` | `passthrough` or `compare`. In `compare` the proxy also streams the same audio to Speech-to-Phrase and logs both transcripts; Home Assistant still gets Whisper's answer unless `DCV_DECISION=enforce`. `compare` needs `DCV_PHRASE_URI` |
| `DCV_PHRASE_URI` | empty | Speech-to-Phrase's Wyoming address, e.g. `tcp://192.168.1.40:10301` (the app's host port is off by default; enable it on the app's Network page) |
| `DCV_PHRASE_TIMEOUT` | `3.0` s | How long, from the end of the audio, the proxy waits for Speech-to-Phrase. Without `enforce` the run log line is written after this wait, so the connection is held up to that long (plus 0.5 s) after HA has its answer. With `enforce` it is the longest a command Whisper got wrong can be held for Speech-to-Phrase |
| `DCV_DECISION` | `off` | `off`, `shadow` or `enforce`. In `shadow` the proxy asks Home Assistant, after the answer has gone out, which of the candidate texts it could act on, and logs the `decision` it *would* have made; Home Assistant still gets Whisper's text. In `enforce` Home Assistant gets the text the decision picks (see below). Both need the three settings below. Run `shadow` first |
| `DCV_HA_URL` | empty | Home Assistant's websocket address, e.g. `ws://192.168.1.30:8123/api/websocket`. `ws://` sends the token unencrypted (the proxy warns); use `wss://` or a network you control |
| `DCV_HA_TOKEN_FILE` | empty | Path of a file holding a Home Assistant long-lived access token (plain ASCII, one token). Read at start-up, and again only after Home Assistant rejects it; never logged, never taken from the command line. **Use a token of a non-admin user**: the call the proxy makes does not need admin rights (read from Home Assistant's source, release 2026.11.0, and tested with a non-admin user on a dev instance). See SECURITY.md for how to protect the file |
| `DCV_HA_TIMEOUT` | `1.5` s | Time allowed for each Home Assistant check (above 0, at most 10). After a failure the proxy stops asking for 5 s, doubling to 60 s, once per outage however many runs hit it; a rejected token is not retried for 5 minutes. An error reply to one request does not start a pause. Over-long or idle connections to Home Assistant are reopened once automatically |

Only 16 kHz, 16-bit, mono audio is accepted, which is what Home Assistant sends. The proxy rebuilds what it forwards to Whisper from validated fields (the language must be letters, digits, `_` or `-`, up to 16 characters, else it is dropped); nothing else a client sends reaches Whisper. The size cap counts at least 4 KB per clip, the cost of a disk block.
Anything else gets an empty transcript and the connection is closed.

## Network exposure (read this)

Wyoming has no authentication. The peer allowlist limits which IP addresses may
connect; it is **not** authentication. Anyone who can forge or take over Home
Assistant's address on the same network segment is let in. Keep the proxy's port on
a network that only trusted devices can reach (a dedicated VLAN or a firewall rule
is better than the allowlist alone).

- Run it with host networking and set `DCV_LISTEN_URI=tcp://<this server's LAN IP>:10310`,
  so only that address listens and the proxy sees Home Assistant's real IP. Bridge
  networking can show the Docker gateway (for example `172.17.0.1`) as the client.
  **Never put the Docker gateway in `DCV_ALLOWED_PEERS`**: every connection from every
  host arrives from it, so the allowlist would let everyone in.
- Connections from loopback and from the proxy's own listen address are always allowed
  (the health check needs that). With host networking this means every process, and every
  other host-network container, on the same machine is trusted.
- IPv6 zone ids are ignored: allowing `fe80::1` admits that address on any interface.
- Not tested: reachability from another VLAN.
- Compare mode and the run log keep household audio and transcripts on disk (see Logs).

## Logs

One JSON line per run on stdout: time, run id, device, peer (client IP), mode, Whisper text,
which engine's text was used, latency after the end of audio, `error`,
`audio_error`, and the path of the saved WAV. The command's audio is held in memory while
it is spoken and written to disk the moment it ends, before the proxy waits for Whisper's
answer, and the write runs alongside the decode so a slow disk cannot delay the answer.
If the container is killed in the middle of a command, that command's audio is lost.
The log line is written after the answer. A command whose client disconnects
mid-way is logged with `"error": "client_disconnected"` and its audio is kept.
A command that never ends (longer than `DCV_MAX_CAPTURE_S` plus the idle time)
is cut off, answered with what was heard so far, and logged.

## The shadow decision

With `DCV_DECISION=shadow` each run's log line gains a `decision`: which text would be used and
why. The rule: Whisper's text if Home Assistant can act on it; else the command sentence found
inside Whisper's text (a command spoken between TV dialogue); else Speech-to-Phrase's text (compare
mode); else Whisper's text unchanged. "Can act on it" means Home Assistant's own sentence matcher
(its websocket `conversation/agent/homeassistant/debug` call, which runs nothing) matched it and,
if it names a device or area, resolved it to at least one device that is exposed to voice.
Home Assistant reports a match for a name it knows even when that device is not exposed, with no
target; the proxy does not count that.

In `shadow` nothing here changes what Home Assistant receives. The check runs after Home Assistant
has its answer, so it never delays it, but it keeps the connection open for up to `DCV_HA_TIMEOUT` +
0.5 s more, after any wait for Speech-to-Phrase.

### Enforcing

With `DCV_DECISION=enforce` the answer waits for the check, in this order:

1. The audio goes to Whisper and to Speech-to-Phrase (compare mode) at the same time, as it arrives.
2. When Whisper finishes, its text and the commands inside it are checked at once (about 40 ms on a warm connection; the first check after a start also connects and logs in).
3. If Whisper's text is actionable, or a command inside it is, that is the answer. Speech-to-Phrase is
   **not** waited for; its run is cancelled once the answer is sent and the run logged (the log shows
   `phrase_error: not_needed`).
4. Only otherwise does the proxy wait for Speech-to-Phrase, at most until `DCV_PHRASE_TIMEOUT` after
   the end of the audio, and checks its text. If that is actionable it is the answer, unless Whisper
   heard a negation ("don't", "not", "never" ...) that Speech-to-Phrase's text lacks: its grammar can
   only say commands, so the swap could do the opposite of what was said. Then Whisper's text goes back.
   After Speech-to-Phrase times out or can't be reached, the proxy stops waiting for it for 30 s.
5. If nothing is actionable, or Home Assistant can't be asked, Whisper's text goes back exactly as
   Whisper said it, and nothing waits for Speech-to-Phrase when Home Assistant can't be asked.

When Whisper returns nothing (or is down) and Speech-to-Phrase has a good command, that command is
the answer. The cost: a command Whisper gets wrong waits for Speech-to-Phrase (up to
`DCV_PHRASE_TIMEOUT`); a command Whisper gets right does not wait for anything but the check. The
whole step is bounded (at most `DCV_PHRASE_TIMEOUT` + two Home Assistant checks after the end of the audio, 7 s with the
defaults; or 2 s after Whisper answers, if that is later. Whisper itself is limited by
`DCV_RESULT_TIMEOUT`. A check is skipped, not cut short, when too little of that time is left), and any
failure inside it returns Whisper's text.

In `enforce` the log line gains `answer` (what Home Assistant received) and `used` says which engine it
came from (`whisper`, `whisper_trimmed`, `phrase`, `none`). `phrase_error` says why Speech-to-Phrase's
result is missing: `not_needed` (fast pass), `skipped_ha_down`, `skipped_slow`, `dropped` (its audio
queue overflowed) or `timeout`. If Speech-to-Phrase had already answered, its text is kept.

Limits, on purpose: transcripts are cut to 2000 characters and cleaned to single spaces first (the
`text` in the decision is that cleaned text; in `enforce` Whisper's own text is returned untouched); a
text longer than 512 characters is not sent to Home Assistant (its sentences are); at most 6
sentences are examined; a sentence cut out at the word "turn" is not used when what comes before it
is a negation ("don't turn off ...") or ends in "and", "or", "then", "but", "also" or "plus", because the second
half alone would mean something else.

## When something fails

| Failure | What happens |
|---|---|
| Whisper unreachable, stalled or too slow | HA gets an empty transcript (it says it didn't understand) within about `DCV_RESULT_TIMEOUT`. The error is logged and the audio is still saved |
| Whisper down at health check | Describe fails, container shows unhealthy. Don't wire autoheal/auto-restart to this check, or it will restart-loop while Whisper is down |
| Audio folder not writable | Loud error at startup, and every run logs `audio_error: audio_save_failed`. The run itself still completes |
| Disk write slower than 15 s | The run completes and logs `audio_save_slow`. The file may still appear later, named with the run id |
| Audio folder full (cap) | The oldest audio is deleted to make room |
| Whisper down when HA loads or reloads the engine | HA gets no engine info, so the engine shows unavailable (describe fails closed on purpose, so the health check is honest) |
| A LAN host holds connections open | Capped per IP, in total and by connection lifetime. There is no authentication, so keep the port off untrusted networks |
| Speech-to-Phrase down, slow, hung or dropping (compare mode) | Nothing changes for HA: it gets Whisper's answer on time. The log line shows `phrase_error` (`connect: ...`, `timeout`, `closed`, `error`) and `phrase_matched: false`. The shadow runs in its own task, so it cannot block the Whisper path |
| Whisper or Speech-to-Phrase sends a reply over the size limits (64 KB of header data, 64 KB payload; 1 MB for the describe reply) | Treated as an error: HA gets an empty transcript, the run is logged with `read: ProtocolError`, and nothing is buffered past the limit |
| Home Assistant down, slow, refusing the token or sending nonsense (`DCV_DECISION=shadow` or `enforce`) | The decision is logged as `whisper_raw` with `ha.available: false`. Home Assistant gets Whisper's text, as without the proxy's decision, and in `enforce` nothing waits for Speech-to-Phrase |
| Unsupported audio format | Empty transcript, connection closed |
| Malformed or oversized message | Nothing is read past the limits (header data 16 KB, audio chunk 64 KB). If a command was open, it is answered with what was heard so far; then the connection is closed |
| Proxy down | HA shows the engine unavailable. Keep a second pipeline on plain Whisper as the fallback |

## Engine name in Home Assistant

The proxy relays Whisper's description but reports its own name,
`dcv-stt-chain-<DCV_DEVICE>`, so Home Assistant's engine list can tell it apart
from Whisper itself. Changing `DCV_DEVICE` later changes that name. Whether Home
Assistant then treats it as a new engine is not yet verified.

## Health check

The container health check does a Wyoming `describe` round trip through the
proxy to Whisper, not just an open-port test. It connects to the address in
`DCV_LISTEN_URI` (or 127.0.0.1 when that is 0.0.0.0).

## Install (Docker)

Docker is the supported way to run it today. A Home Assistant app is planned, not built.

```
docker build -t dcv-stt-chain:0.1.0 stt-chain/

docker run -d --name dcv-stt-chain --network host \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  --memory 512m --pids-limit 128 --user 99:100 \
  --log-opt max-size=10m --log-opt max-file=3 \
  -e DCV_LISTEN_URI=tcp://192.168.1.20:10310 \
  -e DCV_WHISPER_URI=tcp://192.168.1.10:10300 \
  -e DCV_ALLOWED_PEERS=192.168.1.30 \
  -v /path/to/audio:/data/audio \
  dcv-stt-chain:0.1.0
```

Replace the three addresses with this server's LAN IP, Whisper's address and Home
Assistant's address. Then in Home Assistant add a Wyoming Protocol integration entry
for this server's IP and port 10310, and pick it as the speech-to-text engine in a
voice assistant. The mapped audio folder must be writable by the user in `--user`.
The Unraid template in `../unraid/` sets the same options. The base image is pinned by
version and digest, and Python packages by hash.

## Tests

```
pip install -r requirements-dev.txt   # from the repository root
cd stt-chain && pytest
cd ../tools && pytest
```
