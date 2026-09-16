#!/usr/bin/env python3
"""Isolated VLESS/WS Psiphon data-plane contract.

This test deliberately uses a local SOCKS5 test double, *not* an imitation of
Psiphon Core. It proves Lumen's standard VLESS-over-WebSocket endpoint routes
only to the managed loopback SOCKS backend, preserves the primary WS route,
shares the account UUID in its additive subscription profile, and fails closed
when the manager is unavailable. Live validation still requires an official
Core binary, a valid client configuration, and authorized server entries.
"""
from __future__ import annotations

import asyncio
import os
import socket
import sys
import tempfile
import time
import uuid as uuidlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STATE = tempfile.TemporaryDirectory(prefix="lumen-psiphon-ws-")
os.environ.update(
    {
        "DATA_DIR": str(Path(STATE.name) / "state"),
        "PORT": "8897",
        "ADMIN_PASSWORD": "psiphon-ws-test",
        "PSIPHON_ENABLED": "false",
    }
)

import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

import main
import relay_psiphon
import relay_vless
from psiphon.models import PsiphonBackendLease


def vless_header(uid: str, host: str, port: int, payload: bytes = b"") -> bytes:
    """A standard VLESS v0 TCP request header (domain address form)."""
    encoded = host.encode("idna")
    return (
        b"\x00" + uuidlib.UUID(uid).bytes + b"\x00\x01"
        + port.to_bytes(2, "big") + b"\x02" + bytes((len(encoded),))
        + encoded + payload
    )


class FakePsiphonManager:
    """Only the manager contract required by this route; no Core behavior."""

    def __init__(self, socks_port: int):
        self.socks_port = socks_port
        self.settings = SimpleNamespace(connect_timeout_seconds=2)
        self.available = True
        self.started = False
        self.stopped = False
        self.acquires = 0
        self.releases = 0
        self._leases: set[str] = set()

    def set_callbacks(self, **_kwargs) -> None:
        pass

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True
        self.available = False

    def vless_transport_available(self) -> bool:
        return self.available

    async def acquire_vless_backend(self) -> PsiphonBackendLease | None:
        if not self.available:
            return None
        self.acquires += 1
        lease = PsiphonBackendLease(
            lease_id=f"lease-{self.acquires}",
            generation=1,
            host="127.0.0.1",
            port=self.socks_port,
        )
        self._leases.add(lease.lease_id)
        return lease

    async def release_vless_backend(self, lease: PsiphonBackendLease | None) -> None:
        if lease is not None:
            self._leases.discard(lease.lease_id)
            self.releases += 1


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, RuntimeError):
            pass


async def receive_vless_payload(ws, expected: bytes) -> None:
    """Read and verify the ordinary two-byte VLESS response prefix plus data."""
    prefix = b""
    payload = b""
    for _ in range(8):
        frame = await asyncio.wait_for(ws.recv(), timeout=3)
        assert isinstance(frame, bytes), f"unexpected text frame: {frame!r}"
        if not prefix and frame.startswith(b"\x00\x00"):
            prefix = b"\x00\x00"
            frame = frame[2:]
        payload += frame
        if expected in payload:
            break
    assert prefix == b"\x00\x00", f"missing VLESS response prefix: {payload!r}"
    assert expected in payload, f"expected {expected!r}, got {payload!r}"


async def run() -> None:
    destination_hits: list[bytes] = []
    socks_targets: list[tuple[str, int]] = []

    async def destination(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        destination_hits.append(await reader.read(64 * 1024))
        writer.write(b"PSIPHON-BACKEND-OK")
        await writer.drain()
        writer.close()

    destination_server = await asyncio.start_server(destination, "127.0.0.1", 0)
    destination_port = destination_server.sockets[0].getsockname()[1]

    async def socks5(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        try:
            version, methods = await asyncio.wait_for(reader.readexactly(2), 2)
            assert version == 5
            await asyncio.wait_for(reader.readexactly(methods), 2)
            writer.write(b"\x05\x00")
            await writer.drain()

            version, command, reserved, address_type = await asyncio.wait_for(
                reader.readexactly(4), 2,
            )
            assert (version, command, reserved) == (5, 1, 0)
            if address_type == 3:
                size = (await asyncio.wait_for(reader.readexactly(1), 2))[0]
                target = (await asyncio.wait_for(reader.readexactly(size), 2)).decode("idna")
            elif address_type == 1:
                target = socket.inet_ntoa(await asyncio.wait_for(reader.readexactly(4), 2))
            elif address_type == 4:
                target = socket.inet_ntop(socket.AF_INET6, await asyncio.wait_for(reader.readexactly(16), 2))
            else:
                raise AssertionError("unexpected SOCKS address type")
            target_port = int.from_bytes(
                await asyncio.wait_for(reader.readexactly(2), 2), "big",
            )
            socks_targets.append((target, target_port))
            target_reader, upstream = await asyncio.open_connection("127.0.0.1", target_port)
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            await asyncio.gather(pipe(reader, upstream), pipe(target_reader, writer))
        except (AssertionError, ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            writer.close()
            if upstream is not None:
                upstream.close()

    socks_server = await asyncio.start_server(socks5, "127.0.0.1", 0)
    socks_port = socks_server.sockets[0].getsockname()[1]
    fake_manager = FakePsiphonManager(socks_port)
    previous_manager = main.PSIPHON_MANAGER
    main.PSIPHON_MANAGER = fake_manager

    # A Psiphon route must not reach the normal selection/open-outbound path.
    normal_backend_calls: list[str] = []

    async def forbidden_normal_backend(*_args, **_kwargs):
        normal_backend_calls.append("called")
        raise AssertionError("Psiphon route used normal outbound backend")

    previous_open = relay_vless.open_outbound
    previous_resolve = relay_vless._resolve_exact_selection
    relay_vless.open_outbound = forbidden_normal_backend
    relay_vless._resolve_exact_selection = forbidden_normal_backend

    server: uvicorn.Server | None = None
    server_task: asyncio.Task | None = None
    try:
        main.LINKS.clear()
        main.SUBS.clear()
        uid, link = await main.make_link(label="Psiphon WS isolation")
        assert link["protocol"] == "vless-ws"

        # The specific route must win before the generic UUID capture route.
        route_handlers = {
            route.path: route.endpoint
            for route in main.app.routes
            if getattr(route, "path", None) in {"/ws/{uuid}", "/ws/p-core/cq/{uuid}"}
        }
        assert route_handlers["/ws/{uuid}"] is relay_vless.websocket_tunnel
        assert route_handlers["/ws/p-core/cq/{uuid}"] is relay_psiphon.psiphon_websocket_tunnel
        assert route_handlers["/ws/{uuid}"] is not route_handlers["/ws/p-core/cq/{uuid}"]

        # A disabled or unhealthy Core never changes legacy subscription output.
        fake_manager.available = False
        legacy_entries = main.vless_entries_for_link(link, uid, "relay.example")
        assert len(legacy_entries) == 1
        assert f"/ws/{uid}%3Fed%3D4096" in legacy_entries[0]["vless_link"]
        fake_manager.available = True

        # The additive profile is an ordinary VLESS+WS URI and shares the
        # account UUID. No loopback endpoint or Core detail is serialized.
        entries = main.vless_entries_for_link(link, uid, "relay.example")
        assert len(entries) == 2 and entries[1]["profile"] == "psiphon"
        standard, psiphon = entries
        standard_url = urlsplit(standard["vless_link"])
        psiphon_url = urlsplit(psiphon["vless_link"])
        standard_qs = parse_qs(standard_url.query)
        psiphon_qs = parse_qs(psiphon_url.query)
        assert standard_url.scheme == psiphon_url.scheme == "vless"
        assert standard_url.username == psiphon_url.username == uid
        assert standard_qs["type"] == psiphon_qs["type"] == ["ws"]
        assert standard_qs["path"] == [f"/ws/{uid}?ed=4096"]
        assert psiphon_qs["path"] == [f"/ws/p-core/cq/{uid}"]
        assert "psiphon://" not in psiphon["vless_link"].lower()
        for forbidden in ("127.0.0.1", "socks", "password", "proxy"):
            assert forbidden not in psiphon["vless_link"].lower()

        server = uvicorn.Server(
            uvicorn.Config(main.app, host="127.0.0.1", port=8897, log_level="error", ws="auto"),
        )
        server_task = asyncio.create_task(server.serve())
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            await asyncio.sleep(0.025)
        assert server.started, "Uvicorn did not start"
        assert fake_manager.started

        payload = b"ordinary-vless-ws-over-psiphon"
        async with websockets.connect(
            f"ws://127.0.0.1:8897/ws/p-core/cq/{uid}", max_size=2**22,
        ) as ws:
            await ws.send(vless_header(uid, "target.example", destination_port, payload))
            await receive_vless_payload(ws, b"PSIPHON-BACKEND-OK")
        assert socks_targets == [("target.example", destination_port)]
        assert destination_hits == [payload]
        cleanup_deadline = time.monotonic() + 2
        while fake_manager.releases != 1 and time.monotonic() < cleanup_deadline:
            await asyncio.sleep(0.01)
        assert (
            fake_manager.acquires == 1 and fake_manager.releases == 1
        ), (fake_manager.acquires, fake_manager.releases, fake_manager._leases)
        assert not fake_manager._leases, fake_manager._leases
        assert not normal_backend_calls, "Psiphon route fell back to normal backend"

        # UUID authorization remains identical to the protected WS path.
        invalid_calls = fake_manager.acquires
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:8897/ws/p-core/cq/{uuidlib.uuid4()}", max_size=2**22,
            ) as ws:
                await ws.send(vless_header(str(uuidlib.uuid4()), "target.example", destination_port))
                await asyncio.wait_for(ws.recv(), 2)
                raise AssertionError("invalid UUID unexpectedly stayed open")
        except ConnectionClosed as exc:
            assert exc.code == 1008
        assert fake_manager.acquires == invalid_calls

        # An unavailable Core must fail only this endpoint, with no direct or
        # normal-proxy fallback.
        fake_manager.available = False
        unavailable_calls = fake_manager.acquires
        try:
            async with websockets.connect(
                f"ws://127.0.0.1:8897/ws/p-core/cq/{uid}", max_size=2**22,
            ) as ws:
                await ws.send(vless_header(uid, "target.example", destination_port))
                await asyncio.wait_for(ws.recv(), 2)
                raise AssertionError("unavailable Psiphon backend unexpectedly relayed")
        except ConnectionClosed as exc:
            assert exc.code == 1013
        assert fake_manager.acquires == unavailable_calls
        assert not normal_backend_calls
        assert socks_targets == [("target.example", destination_port)]
    finally:
        relay_vless.open_outbound = previous_open
        relay_vless._resolve_exact_selection = previous_resolve
        if server is not None:
            server.should_exit = True
        if server_task is not None:
            await asyncio.wait_for(server_task, 10)
        assert fake_manager.stopped, "application shutdown did not stop the isolated manager"
        main.PSIPHON_MANAGER = previous_manager
        for running in (socks_server, destination_server):
            running.close()
            await running.wait_closed()
        STATE.cleanup()

    print(
        "psiphon WS transport: standard-uri=OK same-uuid=OK "
        "loopback-socks-only=OK normal-backend-isolated=OK "
        "invalid-uuid=closed unavailable=fail-closed"
    )


asyncio.run(run())