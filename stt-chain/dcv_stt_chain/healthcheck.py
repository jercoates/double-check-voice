"""Docker health check: a Wyoming describe round trip through the proxy."""

import asyncio
import os
import sys
from urllib.parse import urlparse

from wyoming.client import AsyncClient
from wyoming.info import Describe, Info


async def _check(uri: str) -> bool:
    client = AsyncClient.from_uri(uri)
    await asyncio.wait_for(client.connect(), 3)
    try:
        await client.write_event(Describe().event())
        event = await asyncio.wait_for(client.read_event(), 8)
        return event is not None and Info.is_type(event.type) and bool(Info.from_event(event).asr)
    finally:
        await client.disconnect()


def health_uri(listen_uri: str) -> str:
    listen = urlparse(listen_uri)
    # Bound to one address (host networking): that address is the only way in.
    host = listen.hostname
    if host in (None, "", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"  # an IPv6 literal needs brackets in a URI
    return f"tcp://{host}:{listen.port}"


def main() -> None:
    uri = health_uri(os.environ.get("DCV_LISTEN_URI", "tcp://0.0.0.0:10310"))
    try:
        ok = asyncio.run(_check(uri))
    except Exception:  # noqa: BLE001
        ok = False
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
