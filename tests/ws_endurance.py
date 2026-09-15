#!/usr/bin/env python3
"""Bounded loopback endurance test for the protected WebSocket/VLESS path.

Default duration is 30 minutes. It keeps one bidirectional stream alive while
also exercising reconnect cycles, then verifies that descriptors, relay
sessions, and asyncio tasks return to their baseline.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 30 * 60
saved = sys.argv[:]
sys.argv = [sys.argv[0], str(ROOT)]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws_hyper_stress as H
sys.argv = saved


async def long_lived(gateway_port: int, target_port: int, until: float) -> tuple[int, int]:
    reader, writer = await H.handshake(gateway_port)
    mask = b"\0\0\0\0"
    header = H.vless_header(target_port, b"endurance-start")
    writer.write(H.ws_header(len(header), True) + mask + header)
    await writer.drain()
    opcode, response = await H.read_ws_frame(reader)
    assert opcode == 2 and response == b"\0\0"
    opcode, initial_echo = await H.read_ws_frame(reader)
    assert opcode == 2 and initial_echo == b"endurance-start"
    bytes_sent = 0
    cycles = 0
    try:
        while time.monotonic() < until:
            payload = (b"lumen-endurance-" + cycles.to_bytes(4, "big")) * 2048
            writer.write(H.ws_header(len(payload), True) + mask + payload)
            await writer.drain()
            opcode, echoed = await asyncio.wait_for(H.read_ws_frame(reader), 10)
            assert opcode == 2 and echoed == payload
            bytes_sent += len(payload)
            cycles += 1
            await asyncio.sleep(min(2.0, max(0.0, until - time.monotonic())))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    return cycles, bytes_sent


async def reconnecting(gateway_port: int, target_port: int, until: float) -> tuple[int, int]:
    cycles = 0
    bytes_sent = 0
    while time.monotonic() < until:
        payload = 64 * 1024
        bytes_sent += await H.client_case(gateway_port, target_port, payload, (cycles % 250) + 1)
        cycles += 1
        await asyncio.sleep(min(5.0, max(0.0, until - time.monotonic())))
    return cycles, bytes_sent


async def main():
    target, target_port = await H.echo_server()
    gateway, gateway_port = await H.gateway_server()
    fd_before = len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").is_dir() else 0
    started = time.monotonic()
    try:
        await H.client_case(gateway_port, target_port, 1024, 1)
        until = started + DURATION
        long_result, reconnect_result = await asyncio.gather(
            long_lived(gateway_port, target_port, until),
            reconnecting(gateway_port, target_port, until),
        )
        await asyncio.sleep(0.2)
        fd_after = len(os.listdir("/proc/self/fd")) if fd_before else 0
        live = [
            task for task in asyncio.all_tasks()
            if task is not asyncio.current_task() and not task.done()
        ]
        assert not H.main_stub.connections, H.main_stub.connections
        assert not H.main_stub.error_logs, H.main_stub.error_logs
        assert not fd_before or fd_after <= fd_before + 3, (fd_before, fd_after)
        assert len(live) <= 2, [task.get_name() for task in live]
        elapsed = time.monotonic() - started
        print(
            f"ws endurance: elapsed={elapsed:.1f}s long_lived_cycles={long_result[0]} "
            f"reconnect_cycles={reconnect_result[0]} bytes={long_result[1] + reconnect_result[1]} "
            f"active=0 errors=0 fd={fd_before}->{fd_after} tasks={len(live)} OK"
        )
    finally:
        gateway.close()
        target.close()
        await gateway.wait_closed()
        await target.wait_closed()


asyncio.run(main())