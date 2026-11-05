"""Entry point: python -m dcv_stt_chain"""

import asyncio
import logging
import os
from functools import partial
from urllib.parse import urlparse

from wyoming.server import AsyncServer

from . import audio_store
from .config import Config
from .ha_check import HaChecker, read_token
from .handler import Shared, SttChainHandler

_LOGGER = logging.getLogger(__name__)


async def _purge_loop(cfg: Config, shared: Shared) -> None:
    loop = asyncio.get_running_loop()
    max_bytes = cfg.audio_max_mb * 1024 * 1024
    while True:
        try:
            async with shared.purge_lock:
                await loop.run_in_executor(
                    audio_store.IO_POOL,
                    audio_store.purge_old,
                    cfg.audio_dir,
                    cfg.audio_retention_days,
                    max_bytes,
                )
                shared.audio_bytes = await loop.run_in_executor(
                    audio_store.IO_POOL, audio_store.dir_size, cfg.audio_dir
                )
        except Exception as err:  # noqa: BLE001 - retention must keep running
            _LOGGER.error("purge loop error: %s", type(err).__name__)
        await asyncio.sleep(3600)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
    )
    os.umask(0o027)  # saved audio and its folders are never world-readable, even briefly
    try:
        cfg = Config.from_env()
    except KeyError as err:
        _LOGGER.error("configuration error: %s is not set", err.args[0])
        raise SystemExit(2) from None
    except ValueError as err:
        _LOGGER.error("configuration error: %s", err)
        raise SystemExit(2) from None
    host = urlparse(cfg.listen_uri).hostname
    if host in ("0.0.0.0", "::", ""):
        _LOGGER.warning(
            "listening on ALL interfaces (%s). Wyoming has no auth: restrict this "
            "port to the HA-facing network (see README)",
            cfg.listen_uri,
        )
    if not cfg.allowed_peers:
        _LOGGER.warning(
            "DCV_ALLOW_ANY_PEER is set and DCV_ALLOWED_PEERS is empty: any host that can "
            "reach this port may connect. Set DCV_ALLOWED_PEERS to Home Assistant's IP address"
        )
    checker = None
    if cfg.decision != "off":
        try:
            token = read_token(cfg.ha_token_file)  # fail at start-up, never echoing the token
        except ValueError as err:
            _LOGGER.error("configuration error: DCV_HA_TOKEN_FILE: %s", err)
            raise SystemExit(2) from None
        if urlparse(cfg.ha_url).scheme == "ws":
            _LOGGER.warning(
                "DCV_HA_URL is ws://: the Home Assistant token is sent unencrypted. Use wss:// "
                "or keep this link on a network you control"
            )
        checker = HaChecker(cfg.ha_url, cfg.ha_token_file, cfg.ha_timeout, token=token)
    audio_store.check_writable(cfg.audio_dir)
    shared = Shared(
        max_connections=cfg.max_connections,
        max_per_ip=cfg.max_per_ip,
        audio_bytes=audio_store.dir_size(cfg.audio_dir),
        ha=checker,
    )
    purge = asyncio.create_task(_purge_loop(cfg, shared))
    server = AsyncServer.from_uri(cfg.listen_uri)
    _LOGGER.info(
        "dcv-stt-chain listening on %s -> %s (device=%s)",
        cfg.listen_uri,
        cfg.whisper_uri,
        cfg.device,
    )
    try:
        await server.run(partial(SttChainHandler, cfg, shared=shared))
    finally:
        purge.cancel()
        if checker is not None:
            await checker.close()


if __name__ == "__main__":
    asyncio.run(main())
