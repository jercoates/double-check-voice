# Contributing and reviewing

Reviews, bug reports and ideas are welcome. This is a small hobby project, so keep changes small
and say what you tested.

## Run the tests

```
pip install -r requirements-dev.txt
cd stt-chain && pytest
cd ../tools && pytest
```

The tests use real TCP sockets against small fake Whisper and Speech-to-Phrase servers. They need
no Home Assistant and no GPU.

## Project rules (please keep to them)

- **Fail safe.** A dead, slow or hostile layer must degrade to plain pass-through to Whisper, never
  to silence or a hang. Every read and write that touches a network peer has a timeout.
- **Shadow first.** New decision logic ships in a mode that decides and logs but does not act, until
  logs from real use justify enforcing it.
- **Log the raw audio before processing**, with the device name and the settings used, so a change
  can be replayed against the same clips.
- **Local and LAN only.** No cloud service and no language model in the live audio path.
- **Do not modify upstream projects in place.** Whisper, Speech-to-Phrase and Home Assistant run as
  they are. If something must be vendored, keep its license and record the source commit in
  [THIRD_PARTY.md](THIRD_PARTY.md).
- **Pin what you depend on** (image digest, package hashes) and upgrade on a test machine first.
- **Hardware-neutral.** Nothing may only work with one microphone or one satellite.
- Settings come from environment variables; secrets from files (`*_FILE`), never the command line.
- A test must be able to fail: check that a new test fails when the fix is removed.

## If you are reviewing the code

Start with `stt-chain/dcv_stt_chain/handler.py`: the Wyoming event flow, the shadow (Speech-to-Phrase)
task, the size-capped readers, and the answer path. Then `audio_store.py` (what it writes and
deletes) and `config.py` (start-up checks). Useful questions:

- Can any network input make the proxy hang, buffer without bound, or answer Home Assistant late?
- Can the second engine (compare mode) affect Whisper's answer in any way?
- Can the purge ever delete something this proxy did not write?
- Do the tests test what their names say?

## Known open issues (so you do not have to rediscover them)

- Connections from loopback and the proxy's own address are always accepted, so with host networking
  every local process is trusted. Narrowing it to health-check requests only is possible but not done.
- The peer allowlist is an IP filter, not authentication. There is no TLS.
- Replies from the speech servers are read through wyoming's private `_reader` attribute (to apply the
  size caps). wyoming is pinned (1.10.2); an upgrade must re-check this.
- A stalled disk can hold one connection for up to 15 s after Home Assistant has its answer (audio
  writes use their own thread pool so name lookups are not starved).
- If the shadow engine overflows its queue, the log may say `phrase_error: timeout` instead of
  naming the overflow. Practically unreachable (a write failure ends the task first); no end-to-end test.
- Tests use short fixed sleeps before reading logs and files; they may be flaky on a very slow machine.
- Tested only with Python 3.12 (the image) and 3.14, with one microphone type, close-up speech and one room.
  Not tested: other VLANs, far-field speech, IPv6 clients, a real Home Assistant on another platform.
- The Unraid template points at a locally built image; there is no published image yet.
- The decision's negation check is a word list ("don't", "didn't", "never", "not", "stop" ...). It also
  blocks harmless cases ("no problem, turn off the light"), and "turn on the light, please turn off the
  fan" is not recognised as two commands. `enforce` is new: run `shadow` first and read the logs. Home
  Assistant's debug call skips a newer fallback matcher, so it can call a sentence unusable that the
  real pipeline would have accepted; the proxy then falls to Speech-to-Phrase's text only if that passes.
- Speech-to-Phrase 1.4.3 (read in source, not profiled) starts a Kaldi decoder at audio-start, keeps one
  warm spare after every command, has no CPU or thread setting and does not reap a decoder when a client
  leaves early. The proxy always sends it an audio-stop before closing, except in two rare races: a
  cancel while the audio-start itself is still being written, and a cancel during the final stop write.
  Each can strand one decoder on that host.
- A gate cut off while it waits for Speech-to-Phrase does not start the 30 s skip window, so the next
  room may wait the same time again.
- `enforce` has been run against real Whisper, Speech-to-Phrase and a dev Home Assistant on 33 clips
  only; the Speech-to-Phrase wait was too slow on that laptop to measure much. Run `shadow` first.
- GitHub Actions are pinned by commit SHA; update them deliberately.
- On a hung disk, runs whose audio write never finishes leave their pending save (and its audio
  in memory, up to about 1 MB each) until the disk answers. Nothing bounds that count.
- The audio-size counter can drift for up to an hour (the hourly rescan races with an in-flight
  save); the purge at the cap recomputes it, so it never grows past the cap for long.
- At the cap, a purge lists every clip (about 390 bytes each, an estimate); with a very large
  number of tiny clips (up to about 524,000 at the default cap) that could approach 200 MB.

## Ideas that are planned but not built

Failover to a second Whisper, per-device profiles, a Home Assistant app, and a
second-stage wake word check (`dcv-wake-verify`). If you want to work on one, open an issue first.
