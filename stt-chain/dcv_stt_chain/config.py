"""Settings from environment variables."""

import ipaddress
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None


def _parse_peers(raw: str) -> frozenset:
    """Comma-separated IP addresses, validated now so a typo can't silently lock HA out."""
    peers = set()
    for item in (p.strip() for p in raw.split(",")):
        if not item:
            continue
        try:
            ip = ipaddress.ip_address(item.split("%")[0])
        except ValueError:
            raise ValueError(
                f"DCV_ALLOWED_PEERS: {item!r} is not a plain IP address "
                "(no ranges, ports or host names)"
            ) from None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        peers.add(str(ip))
    return frozenset(peers)


def _check_tcp_uri(name: str, uri: str) -> None:
    """Fail at startup, not on every run: only tcp://host:port is supported."""
    try:
        parts = urlparse(uri)
        ok = parts.scheme == "tcp" and bool(parts.hostname) and parts.port is not None
    except ValueError:
        ok = False
    if not ok or not (0 < parts.port < 65536):
        raise ValueError(f"{name} must look like tcp://host:port (got {uri!r})")


def _check_ws_uri(name: str, uri: str) -> None:
    # The value is not echoed back: a pasted address can carry a user name and password.
    try:
        parts = urlparse(uri)
        ok = (
            parts.scheme in ("ws", "wss")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
        )
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        ok = False
    if not ok:
        raise ValueError(
            f"{name} must look like ws://host:8123/api/websocket (no user name, password or query)"
        )


@dataclass(frozen=True)
class Config:
    listen_uri: str
    whisper_uri: str
    device: str
    audio_dir: Path
    audio_retention_days: int
    audio_max_mb: int
    connect_timeout: float
    result_timeout: float
    idle_timeout: float
    max_capture_s: float
    max_connections: int
    max_per_ip: int
    max_connection_s: float
    allowed_peers: frozenset = frozenset()
    run_log_file: bool = True
    mode: str = "passthrough"
    phrase_uri: str = ""
    phrase_timeout: float = 3.0
    allow_any_peer: bool = False
    decision: str = "off"
    ha_url: str = ""
    ha_token_file: str = ""
    ha_timeout: float = 1.5

    @staticmethod
    def from_env() -> "Config":
        device = os.environ.get("DCV_DEVICE", "default")
        # Device name ends up in file names: keep it to safe characters.
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", device):
            raise ValueError("DCV_DEVICE may only use letters, digits, _ . - (max 64)")
        cfg = Config(
            listen_uri=os.environ.get("DCV_LISTEN_URI", "tcp://0.0.0.0:10310"),
            whisper_uri=os.environ["DCV_WHISPER_URI"],
            device=device,
            audio_dir=Path(os.environ.get("DCV_AUDIO_DIR", "/data/audio")),
            audio_retention_days=_int("DCV_AUDIO_RETENTION_DAYS", 30),
            audio_max_mb=_int("DCV_AUDIO_MAX_MB", 2048),
            connect_timeout=_float("DCV_CONNECT_TIMEOUT", 2.0),
            result_timeout=_float("DCV_RESULT_TIMEOUT", 10.0),
            idle_timeout=_float("DCV_IDLE_TIMEOUT", 30.0),
            max_capture_s=_float("DCV_MAX_CAPTURE_S", 30.0),
            max_connections=_int("DCV_MAX_CONNECTIONS", 16),
            max_per_ip=_int("DCV_MAX_PER_IP", 8),
            max_connection_s=_float("DCV_MAX_CONNECTION_S", 120.0),
            allowed_peers=_parse_peers(os.environ.get("DCV_ALLOWED_PEERS", "")),
            run_log_file=os.environ.get("DCV_RUN_LOG_FILE", "1").strip().lower()
            not in ("0", "false", "no", "off"),
            mode=os.environ.get("DCV_MODE", "passthrough").strip().lower(),
            phrase_uri=os.environ.get("DCV_PHRASE_URI", "").strip(),
            phrase_timeout=_float("DCV_PHRASE_TIMEOUT", 3.0),
            allow_any_peer=os.environ.get("DCV_ALLOW_ANY_PEER", "0").strip().lower()
            in ("1", "true", "yes", "on"),
            decision=os.environ.get("DCV_DECISION", "off").strip().lower(),
            ha_url=os.environ.get("DCV_HA_URL", "").strip(),
            ha_token_file=os.environ.get("DCV_HA_TOKEN_FILE", "").strip(),
            ha_timeout=_float("DCV_HA_TIMEOUT", 1.5),
        )
        if cfg.decision not in ("off", "shadow", "enforce"):
            raise ValueError("DCV_DECISION must be off, shadow or enforce")
        if cfg.decision in ("shadow", "enforce"):
            if not (cfg.ha_url and cfg.ha_token_file):
                raise ValueError(
                    f"DCV_DECISION={cfg.decision} needs DCV_HA_URL and DCV_HA_TOKEN_FILE"
                )
            _check_ws_uri("DCV_HA_URL", cfg.ha_url)
        # Wyoming has no authentication, so the safe default is closed: the operator
        # names Home Assistant's address, or says out loud that open access is intended.
        if not cfg.allowed_peers and not cfg.allow_any_peer:
            raise ValueError(
                "DCV_ALLOWED_PEERS is empty. Set it to Home Assistant's IP address "
                "(comma-separated for more than one). To accept any host on purpose, "
                "set DCV_ALLOW_ANY_PEER=1 (not recommended: Wyoming has no authentication)"
            )
        _check_tcp_uri("DCV_LISTEN_URI", cfg.listen_uri)
        _check_tcp_uri("DCV_WHISPER_URI", cfg.whisper_uri)
        if cfg.phrase_uri:
            _check_tcp_uri("DCV_PHRASE_URI", cfg.phrase_uri)
        if cfg.mode not in ("passthrough", "compare"):
            raise ValueError("DCV_MODE must be passthrough or compare")
        if cfg.mode == "compare" and not cfg.phrase_uri:
            raise ValueError(
                "DCV_MODE=compare needs DCV_PHRASE_URI (Speech-to-Phrase's Wyoming address)"
            )
        floats = (
            cfg.connect_timeout,
            cfg.result_timeout,
            cfg.idle_timeout,
            cfg.max_capture_s,
            cfg.max_connection_s,
            cfg.phrase_timeout,
            cfg.ha_timeout,
        )
        if not (0 < cfg.ha_timeout <= 10):
            raise ValueError("DCV_HA_TIMEOUT must be above 0 and at most 10 seconds")
        if not all(math.isfinite(v) and 0 < v <= 3600 for v in floats):
            raise ValueError(
                "DCV_*_TIMEOUT, DCV_MAX_CAPTURE_S and DCV_MAX_CONNECTION_S must be > 0 and <= 3600"
            )
        if min(cfg.audio_retention_days, cfg.audio_max_mb, cfg.max_connections, cfg.max_per_ip) < 0:
            raise ValueError(
                "DCV_AUDIO_RETENTION_DAYS, DCV_AUDIO_MAX_MB, DCV_MAX_CONNECTIONS and "
                "DCV_MAX_PER_IP must be >= 0 (0 = no limit)"
            )
        return cfg
