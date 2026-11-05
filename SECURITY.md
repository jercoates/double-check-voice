# Security

## Reporting a problem

Please report security problems privately, using GitHub's "Report a vulnerability" button on this
repository's Security tab, not in a public issue. Include the version, how you run it, and steps to
reproduce. This is a hobby project run by one person: expect an answer in days, not hours.

## What this software is and is not

`dcv-stt-chain` is a network service that accepts speech audio from Home Assistant and forwards it
to a speech-to-text server. It is meant for a home LAN.

- **Wyoming, the protocol it speaks, has no authentication and no encryption.** Anyone who can
  reach the port can send it audio. The peer allowlist (`DCV_ALLOWED_PEERS`) is an IP address
  filter, not authentication: it does not stop someone who can take over or forge Home Assistant's
  address on your network segment. The proxy refuses to start without an allowlist unless you
  explicitly set `DCV_ALLOW_ANY_PEER=1`.
- Put it on a network that only trusted devices can reach. A dedicated VLAN or a firewall rule that
  allows only Home Assistant to reach the port is better than the allowlist alone.
- It stores raw audio of your household and the transcripts. Protect that folder like any other
  recording of your home.

## What it does to limit damage

- Every network read the proxy makes is size-limited before it is performed, for clients and for the
  speech servers behind it, and every read and write has a timeout. (The replay tool and the health
  check talk only to servers you name, and do not apply the size limits.)
- Per-IP and total connection limits; a connection is closed after a fixed lifetime.
- Saved audio uses fixed, validated file names and is created readable only by its owner and group (the
  container sets umask 027); the cleanup only touches files it wrote itself, and
  does not follow symlinks.
- The container image runs as a non-root user, the documented run command drops all capabilities
  and uses a read-only filesystem, and the base image and Python packages are pinned by digest and
  hash.
- No secrets are needed or read by the current version.

## Known limits

- Connections from the loopback address and from the proxy's own address are always accepted
  (the health check needs it). With host networking, every local process is trusted.
- The audio folder is group-readable by design (mode 0640 files, 0750 folders).
- No TLS. Do not expose the port beyond your LAN.

Supported versions: only the latest release.

## Hardening the rest of your setup

The proxy is one piece. Most of the real protection is around it.

**The main risk is a forged transcript.** Home Assistant acts on whatever text its speech-to-text
engine returns, and Wyoming has no authentication or encryption (this is true of a stock Whisper
setup too). Someone who can impersonate your Whisper server to the proxy, or the proxy to Home
Assistant, can make Home Assistant receive any command. So:

- Keep the proxy, Whisper and Speech-to-Phrase on a network only trusted devices can reach, ideally a
  dedicated VLAN with a firewall rule that allows only Home Assistant to reach them.
- Expose to your voice assistant only what you would be willing to have triggered by a forged or
  mis-heard command (a TV or a visitor can do the same by speaking). Think twice before exposing
  locks, garage doors or alarms.

**Home Assistant.**

- A strong, unique password and two-factor login for every user; do not forward port 8123 to the
  internet. Reach it remotely through a VPN.
- A long-lived access token carries the full rights of the user that created it. Create tokens only
  for what needs them, give each its own limited user where you can, and delete the ones you no
  longer use.
- Keep Home Assistant and its apps updated.

**The Docker host (for example Unraid).**

- Set a strong root password, keep SSH off unless you need it, keep the server updated and off the
  internet, and never mount the Docker socket into this container.
- The saved audio is a recording of your home. Put it on a private share that requires a login, not
  one that guests or the whole LAN can read.
- Update the pinned base image now and then (bump the digest in the Dockerfile, then rebuild) to pick up
  security updates; a plain rebuild reuses the same pinned base.

**This project's own credentials.** The current version needs none: it holds no Home Assistant token.
The planned features that need Home Assistant access (for example checking whether a transcript is a
command Home Assistant understands) will read their token from a file, never from the command line
or logs, and are meant to run with a limited user's token, not an administrator's.
