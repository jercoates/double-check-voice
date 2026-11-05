# double-check-voice

Local voice pipeline add-ons for [Home Assistant](https://www.home-assistant.io/). Two small
services that check each other's work, the way a double check valve only passes flow when both
checks hold.

> **Status: early, v0.1.** One component works and is tested; the rest is planned. Details below.
> Please read [What is and is not built](#what-is-and-is-not-built) before relying on anything.

## Why

Home Assistant's local speech-to-text engines make different mistakes. Speech-to-Phrase only
recognises sentences built from your device names, so it is fast and precise but returns nothing
for anything else. faster-whisper hears almost anything, but can mishear a name ("light" as "lid")
or return a command wrapped in background speech from a TV, which Home Assistant then cannot act on.

`dcv-stt-chain` sits in front of faster-whisper as one more Wyoming speech-to-text engine. It
saves the raw audio of every command, logs what the engines heard, and (in compare mode) asks
Speech-to-Phrase the same question in the background so you can see where each engine is right,
using your own voice, microphones and rooms, before changing how anything is decided.

## What is and is not built

| Part | State |
|---|---|
| `dcv-stt-chain`: Wyoming proxy to faster-whisper, raw audio saved, one JSON log line per command | **Works, tested** |
| `dcv-stt-chain` compare mode: the same audio also goes to Speech-to-Phrase in the background; both answers are logged; Home Assistant still gets Whisper's | **Works, tested** (shadow only: it never changes the answer) |
| `tools/replay.py`: replay saved clips through any number of Wyoming engines and compare | **Works, tested** |
| Audio clean-up presets (`cleanup.py`) | Code and tests exist; **not used by the proxy yet**, only by the replay tool. Early tests showed no benefit on close-up speech from one microphone |
| Deciding which text Home Assistant should get (Whisper's, the command inside it, or Speech-to-Phrase's) from what Home Assistant can act on | **Shadow only**: logged as a `decision`, never used. Tested against a fake Home Assistant and a dev instance; enforcing is not built |
| Trimming a command out of surrounding background speech | Part of the shadow decision above |
| Failing over to a second Whisper when the main one is down | Planned |
| Per-device profiles (one settings file for several microphones) | Planned |
| `dcv-wake-verify`: a second-stage check that can veto a false wake word | Planned, nothing built |
| Home Assistant app (add-on) packaging | Planned. **Docker is the supported install for now** |

Early replay tests on a small set of recorded commands (one satellite, one room, close-up speech)
suggest the two engines make different mistakes, which is why this project exists. Those are not
benchmarks and do not say how it behaves with other microphones, distances or accents. The point of
shipping the logging first is to let you measure your own setup.

## Quick start (Docker)

You need a running faster-whisper Wyoming server (for example the `wyoming-faster-whisper`
project) and Home Assistant. Speech-to-Phrase (the Home Assistant app, 1.4.x) is optional.

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

Use your own addresses: this server, your Whisper server, and Home Assistant. The base image is
pinned, so rebuild now and then (or bump the digest in the Dockerfile) to pick up security updates. Then add a Wyoming
Protocol entry in Home Assistant for this server's address and port 10310. The full settings,
failure behaviour and logging are in [stt-chain/README.md](stt-chain/README.md). An Unraid
template is in [unraid/](unraid/).

The proxy refuses to start without `DCV_ALLOWED_PEERS` (or an explicit `DCV_ALLOW_ANY_PEER=1`),
because Wyoming has no authentication.

## Security and privacy

- **Wyoming has no authentication.** The peer allowlist limits which addresses may connect; it is
  not authentication. Keep the port on a network only trusted devices can reach. See
  [SECURITY.md](SECURITY.md) and the "Network exposure" section of the proxy's README.
- **It stores your household's voice.** Every command is saved as a WAV file (default: kept 30
  days, 2 GB cap) with a log of what was heard, next to it. Treat that folder as private, and never
  post clips publicly.
- Nothing leaves your network: there is no cloud service and no language model in the audio path.

## Layout

```
stt-chain/   the proxy (Dockerfile, code, tests, README)
tools/       replay tool and its tests
unraid/      Unraid Community Applications template
```

## Tests

```
pip install -r requirements-dev.txt
cd stt-chain && pytest
cd ../tools && pytest
```

## Contributing and review

Reviews and bug reports are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) lists what to look at first
and the known open issues.

## License

[MIT](LICENSE). Third-party software this project uses or talks to is listed in
[THIRD_PARTY.md](THIRD_PARTY.md).
