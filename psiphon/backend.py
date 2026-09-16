"""Dedicated Psiphon data-plane backend for the additive VLESS/WS route."""
from __future__ import annotations

import asyncio

from .health import open_loopback_socks_connection
from .manager import PsiphonSessionManager
from .models import PsiphonBackendLease


class PsiphonBackendUnavailable(ConnectionError):
    """The active Core tunnel is unavailable; callers must fail closed."""


class PsiphonVlessBackend:
    """Open TCP destinations only through the current loopback Core SOCKS5."""

    def __init__(self, manager: PsiphonSessionManager):
        self._manager = manager

    async def open(
        self,
        address: str,
        port: int,
        first_packet: bytes,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, PsiphonBackendLease]:
        lease = await self._manager.acquire_vless_backend()
        if lease is None:
            raise PsiphonBackendUnavailable("psiphon unavailable")
        try:
            reader, writer = await open_loopback_socks_connection(
                lease.host,
                lease.port,
                address,
                port,
                self._manager.settings.connect_timeout_seconds,
            )
            if first_packet:
                writer.write(first_packet)
                await asyncio.wait_for(
                    writer.drain(),
                    timeout=self._manager.settings.connect_timeout_seconds,
                )
            return reader, writer, lease
        except BaseException:
            await self._manager.release_vless_backend(lease)
            raise

    async def close(
        self,
        writer: asyncio.StreamWriter | None,
        lease: PsiphonBackendLease | None,
    ) -> None:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
            except (asyncio.TimeoutError, OSError, RuntimeError):
                pass
        await self._manager.release_vless_backend(lease)