"""Isolated VLESS-over-WebSocket path backed only by Psiphon Tunnel Core.

The protected ``relay_vless.websocket_tunnel`` implementation is not changed.
This module reuses its VLESS parser, quota/accounting primitives, and
WebSocket I/O adapter while deliberately avoiding its exact-proxy resolver and
``outbound.open_outbound`` path.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime

from fastapi import WebSocket, WebSocketDisconnect

import main as app_state
from psiphon.backend import PsiphonBackendUnavailable, PsiphonVlessBackend
from relay_vless import (
    WRITE_HW_MAX,
    WRITE_HW_START,
    _WSIO,
    _collect_header,
    _early_data,
    _tune_client_socket,
    _tune_socket,
    _ws_client_ip,
    check_and_use,
    relay_client_to_tcp,
    relay_tcp_to_client,
)


async def _run_psiphon_vless_session(
    io: _WSIO,
    uuid: str,
    *,
    client_ip: str,
    early: bytes = b"",
) -> None:
    """Run standard VLESS/TCP framing over a Psiphon-only outbound backend."""
    async with app_state.LINKS_LOCK:
        link = app_state.LINKS.get(uuid)
    if not app_state.is_link_allowed(link):
        await io.close(code=1008, reason="not authorized")
        return
    if not app_state.is_ip_allowed(link, uuid, client_ip):
        await io.close(code=1008, reason="ip limit reached")
        return

    conn_id = secrets.token_urlsafe(6)
    app_state.connections[conn_id] = {
        "uuid": uuid,
        "ip": client_ip,
        "transport": "vless-ws-psiphon",
        "backend": "psiphon",
        "connected_at": datetime.now().isoformat(),
        "bytes": 0,
    }
    app_state.logger.info(
        "vless-ws-psiphon [%s] uuid=%s… ip=%s initial=%dB total=%d",
        conn_id,
        uuid[:8],
        client_ip,
        len(early),
        len(app_state.connections),
    )

    backend = PsiphonVlessBackend(app_state.PSIPHON_MANAGER)
    writer: asyncio.StreamWriter | None = None
    lease = None
    try:
        # Shared VLESS parser: this keeps command/address/payload validation
        # and the pre-authentication size limit exactly aligned with WS.
        _command, address, port, payload, header_bytes = await _collect_header(
            io, early, prefetch_payload=False,
        )
        if not await check_and_use(uuid, header_bytes):
            await io.close(code=1008, reason="quota/disabled")
            return
        app_state.stats["total_requests"] = int(
            app_state.stats.get("total_requests", 0) or 0,
        ) + 1
        connection = app_state.connections.get(conn_id)
        if connection is not None:
            connection["bytes"] += header_bytes

        # No endpoint, country, proxy ID, or direct fallback is supplied here.
        # The only possible data-plane dial is the manager's loopback Core
        # SOCKS endpoint; an unavailable Core fails this session closed.
        reader, writer, lease = await backend.open(address, port, payload)
        _tune_socket(writer, WRITE_HW_START)

        upload = asyncio.create_task(
            relay_client_to_tcp(
                io,
                writer,
                conn_id,
                uuid,
                write_high_water=WRITE_HW_START,
                write_max_high_water=WRITE_HW_MAX,
            ),
            name=f"vless-ws-psiphon-up-{conn_id}",
        )
        download = asyncio.create_task(
            relay_tcp_to_client(io, reader, conn_id, uuid),
            name=f"vless-ws-psiphon-down-{conn_id}",
        )
        done, pending = await asyncio.wait(
            {upload, download}, return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            error = task.exception()
            if error is not None:
                raise error
        try:
            await io.flush()
        except (ConnectionError, OSError):
            pass
        try:
            await app_state.save_state(rotate=False)
        except TypeError as exc:
            if "rotate" not in str(exc):
                raise
            await app_state.save_state()
    except PsiphonBackendUnavailable:
        app_state.stats["total_errors"] = int(
            app_state.stats.get("total_errors", 0) or 0,
        ) + 1
        app_state.error_logs.append(
            {"error": "psiphon backend unavailable", "time": datetime.now().isoformat()},
        )
        await io.close(code=1013, reason="psiphon unavailable")
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        app_state.stats["total_errors"] = int(
            app_state.stats.get("total_errors", 0) or 0,
        ) + 1
        app_state.error_logs.append(
            {"error": "psiphon connection timeout", "time": datetime.now().isoformat()},
        )
        await io.close(code=1011, reason="psiphon timeout")
    except Exception as exc:
        # A Core/SOCKS error can contain a destination or local address. Keep
        # only its type in logs and never send it to a client or dashboard.
        app_state.stats["total_errors"] = int(
            app_state.stats.get("total_errors", 0) or 0,
        ) + 1
        app_state.error_logs.append(
            {
                "error": "psiphon backend " + type(exc).__name__,
                "time": datetime.now().isoformat(),
            },
        )
        app_state.logger.warning(
            "vless-ws-psiphon failed [%s]: %s", conn_id, type(exc).__name__,
        )
        await io.close(code=1011, reason="psiphon backend failure")
    finally:
        await backend.close(writer, lease)
        app_state.connections.pop(conn_id, None)
        app_state.logger.info(
            "vless-ws-psiphon closed [%s] total=%d",
            conn_id,
            len(app_state.connections),
        )


async def psiphon_websocket_tunnel(ws: WebSocket, uuid: str) -> None:
    """Normal VLESS-over-WS handler for exactly ``/ws/p-core/cq/{uuid}``."""
    early = _early_data(ws)
    await ws.accept()
    _tune_client_socket(ws)
    await _run_psiphon_vless_session(
        _WSIO(ws),
        uuid,
        client_ip=_ws_client_ip(ws),
        early=early,
    )