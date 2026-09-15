"""Reliable per-config HTTP/HTTPS/SOCKS5 outbound connector.

Routing contract (fail-closed):
- a config that selects a proxy ALWAYS exits through a proxy; there is no
  silent fallback to the direct route — a failing proxy fails the connection
  instead of leaking the server's own IP;
- any payload (TLS, plain HTTP, DNS-over-TCP, …) uses the tunnel, so traffic
  type can never decide whether the proxy is used;
- destination domains are always handed to the selected proxy for remote DNS;
- exactly one configured endpoint is accepted, so failure can never switch the
  route to another proxy or to the Railway server.

Compatibility goals retained:
- bounded handshakes prevent hangs on dead proxies;
- HTTPS-list entries support both TLS-to-proxy and plain CONNECT semantics.
"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import socket
import ssl
import time
from urllib.parse import unquote, urlsplit

import proxy_repository as repo

logger = logging.getLogger("Lumen.outbound")
HANDSHAKE_TIMEOUT = 4.0
CONNECT_HEADER_MAX = 32 * 1024
PROBE_BODY_MAX = 64 * 1024
FAILURE_BASE_SECONDS = 10.0
FAILURE_MAX_SECONDS = 300.0

class ProxyUnavailableError(OSError):
    """A proxy was configured for this route but none can currently be used.
    Raised so the caller can fail the connection instead of leaking a direct
    server-side exit."""



_dialer = asyncio.open_connection
_tuner = None
_proxy_tls_context: ssl.SSLContext | None = None

# endpoint -> (consecutive_failures, cooldown_until_monotonic)
_proxy_health: dict[str, tuple[int, float]] = {}


def set_dialer(fn):
    global _dialer
    _dialer = fn


def set_tuner(fn):
    global _tuner
    _tuner = fn


def _tune(writer):
    if _tuner:
        try:
            _tuner(writer)
        except Exception:
            pass


def _managed_proxy_tls_context() -> ssl.SSLContext:
    """Reuse the managed-proxy TLS policy instead of allocating per session."""
    global _proxy_tls_context
    if _proxy_tls_context is None:
        # The existing managed repository is the trust boundary for TLS proxy
        # endpoints. Preserve its historical mismatch-tolerant behavior while
        # avoiding repeated context/cipher-store construction in the hot path.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        _proxy_tls_context = context
    return _proxy_tls_context


async def _dial(host, port):
    return await _dialer(host, port)


def _close(writer) -> None:
    if writer is not None:
        try:
            writer.close()
        except Exception:
            pass


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(str(value).strip("[]").split("%", 1)[0])
        return True
    except ValueError:
        return False


def parse_proxy_url(value):
    parsed = urlsplit(repo.validate_url(value))
    return {
        "scheme": parsed.scheme,
        "hostname": parsed.hostname,
        "port": parsed.port,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
    }


def link_uses_proxy(link) -> bool:
    if not isinstance(link, dict):
        return False
    mode = str(link.get("exit_proxy_mode") or "direct")
    return (mode == "repository" and bool(link.get("proxy_id"))) or (
        mode == "custom" and bool(link.get("custom_proxy"))
    )


def proxy_health_snapshot() -> dict:
    """endpoint -> failure count for endpoints currently cooling down."""
    now = time.monotonic()
    return {ep: fails for ep, (fails, until) in _proxy_health.items() if until > now}


def _record_proxy_success(endpoint: str) -> None:
    _proxy_health.pop(endpoint, None)


def _record_proxy_failure(endpoint: str) -> None:
    fails, _until = _proxy_health.get(endpoint, (0, 0.0))
    fails = min(fails + 1, 8)
    cooldown = min(FAILURE_BASE_SECONDS * (2 ** (fails - 1)), FAILURE_MAX_SECONDS)
    _proxy_health[endpoint] = (fails, time.monotonic() + cooldown)
    if len(_proxy_health) > 4096:
        # Bound memory: drop the entries closest to recovery.
        for key in sorted(_proxy_health, key=lambda k: _proxy_health[k][1])[:512]:
            _proxy_health.pop(key, None)


def _socks_target(host: str, port: int) -> bytes:
    bare = str(host).strip("[]")
    try:
        ip = ipaddress.ip_address(bare.split("%", 1)[0])
        address = (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
    except ValueError:
        encoded = bare.encode("idna")
        if len(encoded) > 255:
            raise ValueError("SOCKS5 target hostname too long")
        address = b"\x03" + bytes([len(encoded)]) + encoded
    return address + int(port).to_bytes(2, "big")


async def _read_socks_reply(reader):
    head = await reader.readexactly(4)
    if head[0] != 5 or head[1] != 0:
        raise OSError("SOCKS5 CONNECT failed: " + str(head[1] if len(head) > 1 else -1))
    if head[3] == 1:
        await reader.readexactly(6)
    elif head[3] == 4:
        await reader.readexactly(18)
    elif head[3] == 3:
        await reader.readexactly((await reader.readexactly(1))[0] + 2)
    else:
        raise OSError("SOCKS5 invalid reply address type")


def _elapsed_ms(started: float) -> float:
    """Return a measured duration without exposing clock details to callers."""
    return round((time.perf_counter() - started) * 1000, 3)


async def _socks_once(target, port, first_packet, params, metrics: dict | None = None):
    started = time.perf_counter()
    reader, writer = await _dial(params["hostname"], params["port"])
    connected_at = time.perf_counter()
    _tune(writer)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            username = params["username"]
            password = params["password"]
            has_auth = bool(username or password)
            writer.write(b"\x05\x02\x00\x02" if has_auth else b"\x05\x01\x00")
            await writer.drain()
            response = await reader.readexactly(2)
            if response[0] != 5:
                raise OSError("SOCKS5 invalid greeting")
            if response[1] == 2:
                if not has_auth:
                    raise OSError("SOCKS5 authentication required")
                user = username.encode()
                secret = password.encode()
                if len(user) > 255 or len(secret) > 255:
                    raise ValueError("SOCKS5 credentials too long")
                writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
                await writer.drain()
                auth = await reader.readexactly(2)
                if auth[0] != 1 or auth[1] != 0:
                    raise OSError("SOCKS5 authentication failed")
            elif response[1] != 0:
                raise OSError("SOCKS5 authentication method rejected")
            writer.write(b"\x05\x01\x00" + _socks_target(target, port))
            await writer.drain()
            await _read_socks_reply(reader)
            if first_packet:
                writer.write(first_packet)
                await writer.drain()
        if metrics is not None:
            metrics["connect_ms"] = round((connected_at - started) * 1000, 3)
            metrics["handshake_ms"] = round((time.perf_counter() - connected_at) * 1000, 3)
        return reader, writer
    except BaseException:
        _close(writer)
        raise


async def _socks_connect(target, port, first_packet, params, metrics: dict | None = None):
    # Keep the hostname inside SOCKS5. Local DNS fallback would leak DNS and can
    # make a tested identity behave differently at runtime.
    return await _socks_once(target, port, first_packet, params, metrics)


def _connect_authority(host: str, port: int) -> str:
    bare = str(host).strip("[]")
    return ("[" + bare + "]" if ":" in bare else bare) + ":" + str(port)


def _connect_request(host: str, port: int, params: dict) -> bytes:
    authority = _connect_authority(host, port)
    lines = [
        "CONNECT " + authority + " HTTP/1.1",
        "Host: " + authority,
        "User-Agent: Mozilla/5.0",
        "Proxy-Connection: keep-alive",
        "Connection: keep-alive",
    ]
    if params["username"] or params["password"]:
        token = base64.b64encode(
            (params["username"] + ":" + params["password"]).encode()
        ).decode()
        lines.append("Proxy-Authorization: Basic " + token)
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _http_once(target, port, first_packet, params, tls_to_proxy: bool, metrics: dict | None = None):
    started = time.perf_counter()
    reader, writer = await _dial(params["hostname"], params["port"])
    connected_at = time.perf_counter()
    _tune(writer)
    try:
        async with asyncio.timeout(HANDSHAKE_TIMEOUT):
            if tls_to_proxy:
                # Public/managed proxy lists commonly contain IP endpoints with
                # self-signed or hostname-mismatched certs. Encryption is kept,
                # while endpoint trust comes from the private managed list.
                server_hostname = None if _is_ip(params["hostname"]) else params["hostname"]
                await writer.start_tls(_managed_proxy_tls_context(), server_hostname=server_hostname)
            writer.write(_connect_request(target, port, params))
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > CONNECT_HEADER_MAX:
                raise OSError("proxy CONNECT header too long")
            fields = header.split(b"\r\n", 1)[0].split()
            code = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else -1
            if not 200 <= code < 300:
                raise OSError("proxy CONNECT failed: HTTP " + str(code))
            if first_packet:
                writer.write(first_packet)
                await writer.drain()
        if metrics is not None:
            metrics["connect_ms"] = round((connected_at - started) * 1000, 3)
            metrics["handshake_ms"] = round((time.perf_counter() - connected_at) * 1000, 3)
        return reader, writer
    except BaseException:
        _close(writer)
        raise


async def _http_connect(target, port, first_packet, params, metrics: dict | None = None):
    if params["scheme"] == "https":
        # Most public `https://IP:port` lists mean an HTTP CONNECT proxy that
        # supports HTTPS destinations, not TLS transport to the proxy itself.
        # Prefer that convention for IPs, but support real TLS proxies too.
        transports = (False, True) if _is_ip(params["hostname"]) else (True, False)
    else:
        transports = (False,)
    last_error = None
    for tls_to_proxy in transports:
        try:
            return await _http_once(
                target, port, first_packet, params, tls_to_proxy=tls_to_proxy,
                metrics=metrics,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
    # Never resolve the destination locally. The selected proxy either accepts
    # the hostname or the connection fails closed.
    raise last_error or OSError("HTTP proxy connection failed")


async def _endpoint_from_link(link) -> str | None:
    """Single endpoint for a per-config exit proxy. Fail-closed: a configured
    proxy that cannot be resolved raises instead of silently going direct."""
    if not isinstance(link, dict):
        return None
    mode = str(link.get("exit_proxy_mode") or "direct")
    if mode == "repository":
        record = await repo.resolve(link.get("proxy_id"))
        if record is None:
            raise ProxyUnavailableError("managed proxy is not in the repository cache")
        return record.endpoint
    if mode == "custom":
        try:
            return repo.validate_url(link.get("custom_proxy"))
        except ValueError as exc:
            raise ProxyUnavailableError("custom proxy URL is invalid") from exc
    return None


async def _open_via(
    endpoint: str,
    address: str,
    port: int,
    packet: bytes,
    metrics: dict | None = None,
):
    params = parse_proxy_url(endpoint)
    if params["scheme"] == "socks5":
        return await _socks_connect(address, port, packet, params, metrics)
    return await _http_connect(address, port, packet, params, metrics)


async def open_outbound(address, port, first_packet=None, *, link=None, uuid="", endpoints=None, proxy_id=""):
    """Open one deterministic upstream path.

    No endpoints means an intentional direct route. Exactly one endpoint means
    the exact selected managed/custom proxy. More than one endpoint is rejected
    because retrying another proxy would violate explicit-selection semantics.
    """
    packet = bytes(first_packet or b"")
    if endpoints is None:
        single = await _endpoint_from_link(link)
        endpoints = [single] if single else []
    exact = list(dict.fromkeys(str(e) for e in endpoints or [] if e))
    if not exact:
        if link_uses_proxy(link):
            raise ProxyUnavailableError("configured proxy did not resolve")
        reader, writer = await _dial(address, port)
        _tune(writer)
        return reader, writer, False
    if len(exact) != 1:
        raise ProxyUnavailableError("explicit routing accepts exactly one proxy endpoint")

    endpoint = exact[0]
    try:
        reader, writer = await _open_via(endpoint, address, port, packet)
        _record_proxy_success(endpoint)
        return reader, writer, True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _record_proxy_failure(endpoint)
        # Log the stable ID only. Never log endpoint URLs or credentials.
        logger.info("selected proxy failed id=%s target=%s:%d error=%s", str(proxy_id or "unknown")[:32], address, port, type(exc).__name__)
        raise OSError(f"selected proxy {str(proxy_id or 'unknown')[:32]} failed closed") from exc


async def _read_http_response(reader, *, include_body: bool) -> tuple[int, dict[str, str], bytes]:
    """Read one bounded HTTP/1.1 response. Probe connections always close."""
    header = await reader.readuntil(b"\r\n\r\n")
    if len(header) > CONNECT_HEADER_MAX:
        raise OSError("HTTPS response header too long")
    lines = header.split(b"\r\n")
    fields = lines[0].split()
    status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.decode("latin-1").strip().lower()] = value.decode("latin-1").strip()
    if not include_body:
        return status, headers, b""

    length = headers.get("content-length")
    if length is not None:
        try:
            body_length = int(length)
        except ValueError as exc:
            raise OSError("invalid HTTPS response content length") from exc
        if body_length < 0 or body_length > PROBE_BODY_MAX:
            raise OSError("HTTPS probe response body too large")
        return status, headers, await reader.readexactly(body_length)

    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        total = 0
        while True:
            line = await reader.readline()
            if not line or len(line) > 128:
                raise OSError("invalid HTTPS chunk header")
            try:
                chunk_size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError as exc:
                raise OSError("invalid HTTPS chunk size") from exc
            if chunk_size < 0 or total + chunk_size > PROBE_BODY_MAX:
                raise OSError("HTTPS probe response body too large")
            if chunk_size == 0:
                # Consume bounded optional trailers.
                trailer_bytes = 0
                while True:
                    trailer = await reader.readline()
                    trailer_bytes += len(trailer)
                    if trailer_bytes > CONNECT_HEADER_MAX:
                        raise OSError("HTTPS response trailers too long")
                    if trailer in (b"", b"\r\n"):
                        return status, headers, b"".join(chunks)
            chunks.append(await reader.readexactly(chunk_size))
            total += chunk_size
            if await reader.readexactly(2) != b"\r\n":
                raise OSError("invalid HTTPS chunk terminator")

    # `Connection: close` makes this a safe fallback for the two small identity
    # endpoints. Limit the read so a malicious response cannot retain memory.
    body = await reader.read(PROBE_BODY_MAX + 1)
    if len(body) > PROBE_BODY_MAX:
        raise OSError("HTTPS probe response body too large")
    return status, headers, body


async def _probe_https_request(
    endpoint: str,
    hostname: str,
    path: str = "/",
    *,
    timeout: float = 10.0,
    include_body: bool = False,
) -> dict:
    """Issue one measured HTTPS request through one exact proxy endpoint."""
    writer = None
    started = time.perf_counter()
    metrics: dict[str, float] = {}
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await _open_via(endpoint, hostname, 443, b"", metrics)
            context = ssl.create_default_context()
            await writer.start_tls(context, server_hostname=hostname)
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {hostname}\r\n"
                "User-Agent: Lumen-Exact-Proxy-Test/30\r\n"
                "Accept: */*\r\nConnection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(request)
            await writer.drain()
            status, _headers, body = await _read_http_response(reader, include_body=include_body)
            total_ms = _elapsed_ms(started)
            # Cloudflare and Google may redirect their root URL. A valid HTTPS
            # response in the 2xx/3xx range proves outbound HTTPS connectivity.
            return {
                "target": "https://" + hostname,
                "ok": 200 <= status < 400,
                "status": status or None,
                "connect_ms": metrics.get("connect_ms"),
                "handshake_ms": metrics.get("handshake_ms"),
                # Includes target TLS plus request/first-response timing after
                # the proxy tunnel was established.
                "request_ms": max(0.0, round(total_ms - sum(metrics.values()), 3)),
                "total_ms": total_ms,
                # Keep the legacy field for existing API consumers.
                "latency_ms": total_ms,
                "body": body if include_body else b"",
            }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        total_ms = _elapsed_ms(started)
        return {
            "target": "https://" + hostname,
            "ok": False,
            "status": None,
            "connect_ms": metrics.get("connect_ms"),
            "handshake_ms": metrics.get("handshake_ms"),
            "request_ms": max(0.0, round(total_ms - sum(metrics.values()), 3)),
            "total_ms": total_ms,
            "latency_ms": total_ms,
            "error": type(exc).__name__,
            "body": b"",
        }
    finally:
        _close(writer)
        if writer is not None:
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _probe_https_target(endpoint: str, hostname: str, timeout: float = 10.0) -> dict:
    """Issue one measured HTTPS GET through one exact proxy endpoint."""
    return await _probe_https_request(endpoint, hostname, timeout=timeout)


async def _exit_identity(endpoint: str, timeout: float) -> dict:
    """Discover public egress identity with requests that also use this proxy."""
    identity = await _probe_https_request(
        endpoint, "api.ipify.org", "/?format=json", timeout=timeout, include_body=True,
    )
    if not identity.get("ok"):
        return {"exit_ip": "", "exit_location": "", "exit_country_code": ""}
    try:
        payload = json.loads(identity.get("body", b"").decode("utf-8"))
        address = str(payload.get("ip") or "").strip()
        ipaddress.ip_address(address)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        return {"exit_ip": "", "exit_location": "", "exit_country_code": ""}

    # Location is diagnostic-only. It remains on the same exact proxy route and
    # cannot make an otherwise successful performance check unhealthy.
    geo = await _probe_https_request(
        endpoint, "ipapi.co", "/" + address + "/json/", timeout=timeout, include_body=True,
    )
    location = ""
    country_code = ""
    if geo.get("ok"):
        try:
            details = json.loads(geo.get("body", b"").decode("utf-8"))
            city = str(details.get("city") or "").strip()
            country = str(details.get("country_name") or details.get("country") or "").strip()
            country_code = str(details.get("country_code") or "").strip().upper()
            location = ", ".join(part for part in (city, country) if part)[:160]
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
    return {
        "exit_ip": address,
        "exit_location": location,
        "exit_country_code": country_code,
    }


async def test_proxy_record(record, timeout: float = 10.0) -> dict:
    """Test both required targets through the exact selected record only."""
    targets = ("cloudflare.com", "google.com")
    results = await asyncio.gather(*(_probe_https_target(record.endpoint, host, timeout) for host in targets))
    ok = all(item.get("ok") for item in results)
    identity = (
        await _exit_identity(record.endpoint, timeout)
        if ok
        else {"exit_ip": "", "exit_location": "", "exit_country_code": ""}
    )
    return {
        "proxy_id": record.id,
        "ok": ok,
        "checks": list(results),
        **identity,
    }
