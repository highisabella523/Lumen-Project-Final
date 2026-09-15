#!/usr/bin/env python3
"""Controlled loopback WS/VLESS concurrency benchmark with cleanup telemetry.

It measures the existing production WebSocket relay path only. The result is a
local process benchmark, not a client-to-Railway RTT claim.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
saved = sys.argv[:]
sys.argv = [sys.argv[0], str(ROOT)]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ws_hyper_stress as H
sys.argv = saved

LEVELS = (1, 10, 50, 100)
MIB_PER_CLIENT = 1


def rss_kib() -> int:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return 0


def p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


async def measure(gateway_port: int, target_port: int, clients: int) -> None:
    payload_size = MIB_PER_CLIENT * 1024 * 1024
    started_cpu, started_rss = time.process_time(), rss_kib()

    async def one(index: int):
        started = time.perf_counter()
        total = await H.client_case(gateway_port, target_port, payload_size, (index % 250) + 1)
        return total, (time.perf_counter() - started) * 1000

    started = time.perf_counter()
    totals = await asyncio.wait_for(asyncio.gather(*(one(i) for i in range(clients))), 180)
    elapsed = time.perf_counter() - started
    await asyncio.sleep(0.1)
    cpu_ms = (time.process_time() - started_cpu) * 1000 / clients
    end_rss = rss_kib()
    fd_count = len(os.listdir("/proc/self/fd")) if Path("/proc/self/fd").is_dir() else 0
    latencies = [item[1] for item in totals]
    sent_mib = sum(item[0] for item in totals) / (1024 * 1024)
    assert not H.main_stub.connections, H.main_stub.connections
    assert not H.main_stub.error_logs, H.main_stub.error_logs
    print(
        f"ws {clients}x{MIB_PER_CLIENT}MiB: success={clients}/{clients} "
        f"p50={statistics.median(latencies):.2f}ms p95={p95(latencies):.2f}ms "
        f"duplex={sent_mib * 2 / elapsed:.1f}MiB/s cpu={cpu_ms:.2f}ms/conn "
        f"rss={end_rss}KiB ({end_rss - started_rss:+d}) fd={fd_count} "
        f"active=0 errors=0"
    )


async def main():
    target, target_port = await H.echo_server()
    gateway, gateway_port = await H.gateway_server()
    try:
        # Warm the actual parser, relay, and route cache before measurements.
        await H.client_case(gateway_port, target_port, 1024, 1)
        for clients in LEVELS:
            await measure(gateway_port, target_port, clients)
    finally:
        gateway.close()
        target.close()
        await gateway.wait_closed()
        await target.wait_closed()


asyncio.run(main())