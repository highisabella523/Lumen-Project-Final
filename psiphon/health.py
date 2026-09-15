"""Bounded health probes through an official Core loopback SOCKS listener."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import ssl
import time
from dataclasses import dataclass

from .models import TunnelHealth, bounded_float, safe_region

_HEADER_LIMIT = 16 * 1024
_BODY_LIMIT = 16 * 1024
_IPIFY_HOST = "api.ipify.org"
_IPIFY_PATH = "/?format=json"
_CONNECTIVITY_HOST = "www.cloudflare.com"
_CONNECTIVITY_PATH = "/cdn-cgi/trace"
_GEO_HOST = "ipapi.co"


@dataclass(frozen=True)
class HTTPResult:
    ok: bool
    status: int | None
    body: bytes
    latency_ms: float


async def _close(writer: asyncio.StreamWriter | None) -> None:
    if writer is None:
        return
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, RuntimeError):
        pass


async def _socks_connect(
    host: str,
    port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, limit=_HEADER_LIMIT), timeout=timeout,
    )
    try:
        writer.write(b"\x05\x01\x00")
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        response = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if response != b"\x05\x00":
            raise ConnectionError("socks_auth_failed")
        encoded = target_host.encode("idna")
        if not (1 <= len(encoded) <= 255):
            raise ConnectionError("invalid_target")
        request = b"\x05\x01\x00\x03" + bytes([len(encoded)]) + encoded + target_port.to_bytes(2, "big")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        head = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if len(head) != 4 or head[0] != 5 or head[1] != 0:
            raise ConnectionError("socks_connect_failed")
        atyp = head[3]
        if atyp == 3:
            address_length = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
        elif atyp == 1:
            address_length = 4
        elif atyp == 4:
            address_length = 16
        else:
            raise ConnectionError("socks_reply_invalid")
        await asyncio.wait_for(reader.readexactly(address_length + 2), timeout=timeout)
        return reader, writer
    except BaseException:
        await _close(writer)
        raise


async def open_loopback_socks_connection(
    socks_host: str,
    socks_port: int,
    target_host: str,
    target_port: int,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open one TCP stream through the Core's loopback-only SOCKS5 listener."""
    if socks_host != "127.0.0.1":
        raise ConnectionError("Psiphon SOCKS must be loopback-only")
    return await _socks_connect(
        socks_host, socks_port, target_host, target_port, timeout,
    )


async def _https_get(
    socks_host: str,
    socks_port: int,
    host: str,
    path: str,
    timeout: float,
) -> HTTPResult:
    started = time.perf_counter()
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await _socks_connect(
            socks_host, socks_port, host, 443, timeout,
        )
        context = ssl.create_default_context()
        await asyncio.wait_for(
            writer.start_tls(
                context,
                server_hostname=host,
                ssl_handshake_timeout=timeout,
            ),
            timeout=timeout,
        )
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
            "User-Agent: Lumen-Psiphon-Health/1\r\nConnection: close\r\n"
            "Accept: application/json,text/plain,*/*\r\n\r\n"
        ).encode("ascii")
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        if len(header) > _HEADER_LIMIT:
            return HTTPResult(False, None, b"", _elapsed_ms(started))
        first = header.split(b"\r\n", 1)[0].split()
        status = int(first[1]) if len(first) >= 2 and first[1].isdigit() else None
        body = await asyncio.wait_for(reader.read(_BODY_LIMIT), timeout=timeout)
        return HTTPResult(bool(status and 200 <= status < 400), status, body, _elapsed_ms(started))
    except (
        asyncio.TimeoutError,
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        OSError,
        ssl.SSLError,
        ValueError,
    ):
        return HTTPResult(False, None, b"", _elapsed_ms(started))
    finally:
        await _close(writer)


async def probe_tunnel(
    socks_host: str,
    socks_port: int,
    timeout: float,
) -> TunnelHealth:
    """Validate actual HTTPS forwarding before reporting a Core session healthy."""
    connectivity = await _https_get(
        socks_host, socks_port, _CONNECTIVITY_HOST, _CONNECTIVITY_PATH, timeout,
    )
    if not connectivity.ok:
        return TunnelHealth(False, connectivity.latency_ms, reason="https_probe_failed")

    identity = await _https_get(socks_host, socks_port, _IPIFY_HOST, _IPIFY_PATH, timeout)
    if not identity.ok:
        return TunnelHealth(True, connectivity.latency_ms, reason="exit_identity_unavailable")
    try:
        parsed = json.loads(identity.body.decode("utf-8", "replace"))
        exit_ip = str(parsed.get("ip") or "").strip()
        ipaddress.ip_address(exit_ip)
    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
        return TunnelHealth(True, connectivity.latency_ms, reason="exit_identity_unavailable")

    geo = await _https_get(
        socks_host, socks_port, _GEO_HOST, f"/{exit_ip}/json/", timeout,
    )
    country = region = ""
    if geo.ok:
        try:
            payload = json.loads(geo.body.decode("utf-8", "replace"))
            country = safe_region(payload.get("country_code"))
            region = str(payload.get("region") or "").strip()[:80]
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
            pass
    return TunnelHealth(
        True,
        bounded_float(connectivity.latency_ms),
        exit_ip=exit_ip,
        exit_country=country,
        exit_region=region,
        reason="",
    )


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)