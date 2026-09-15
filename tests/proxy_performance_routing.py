#!/usr/bin/env python3
"""Exact-proxy performance probes, ranking, country routing, and persistence."""
from __future__ import annotations

import asyncio
import json
import os
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="lumen-performance-")
import sys
sys.path.insert(0, str(ROOT))

import main
import outbound
import proxy_performance
import proxy_repository as repo


async def pipe(reader, writer):
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def run():
    with tempfile.TemporaryDirectory() as td:
        cert, key = Path(td) / "cert.pem", Path(td) / "key.pem"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-days", "1", "-subj", "/CN=localhost", "-keyout", str(key),
             "-out", str(cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        target_log: list[tuple[str, str]] = []
        geo_failure = False

        async def target(reader, writer):
            line = (await reader.readline()).decode("latin-1").strip()
            host = ""
            while True:
                header = await reader.readline()
                if header == b"\r\n":
                    break
                if header.lower().startswith(b"host:"):
                    host = header.decode("latin-1").split(":", 1)[1].strip()
            target_log.append((host, line))
            status = b"HTTP/1.1 200 OK"
            if host == "api.ipify.org":
                body = b'{"ip":"203.0.113.7"}'
            elif host == "ipapi.co":
                if geo_failure:
                    status, body = b"HTTP/1.1 503 Service Unavailable", b"{}"
                else:
                    body = b'{"city":"Frankfurt","country_name":"Germany","country_code":"DE"}'
            else:
                body = b""
            writer.write(
                status + b"\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
            writer.close()

        tls_server = await asyncio.start_server(target, "127.0.0.1", 0, ssl=tls)
        tls_port = tls_server.sockets[0].getsockname()[1]
        logs: dict[str, list[bytes]] = {}
        delays: dict[str, float] = {}
        proxies = []
        records = []

        for index, delay in enumerate((0.001, 0.012, 0.028), 1):
            proxy_id = f"de-{index}"
            logs[proxy_id] = []
            delays[proxy_id] = delay

            async def handler(reader, writer, proxy_id=proxy_id):
                try:
                    header = await reader.readuntil(b"\r\n\r\n")
                    first = header.split(b"\r\n", 1)[0]
                    logs[proxy_id].append(first)
                    await asyncio.sleep(delays[proxy_id])  # real fixture delay, not injected timing
                    _method, authority, _version = first.decode().split()
                    _host, _port_text = authority.rsplit(":", 1)
                    upstream_reader, upstream_writer = await asyncio.open_connection(
                        "127.0.0.1", tls_port
                    )
                    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                    await writer.drain()
                    await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))
                except (ConnectionError, asyncio.IncompleteReadError, ValueError):
                    writer.close()

            server = await asyncio.start_server(handler, "127.0.0.1", 0)
            proxies.append(server)
            port = server.sockets[0].getsockname()[1]
            records.append(repo.Record(
                proxy_id, f"http://127.0.0.1:{port}", "http",
                "Germany", "DE", "🇩🇪", 1,
            ))

        repo._records = {record.id: record for record in records}
        repo._last = time.monotonic()
        repo._error = ""
        main.PROXY_TEST_RESULTS.clear()
        main.PREFERRED_PROXY_BY_COUNTRY.clear()
        original_context = ssl.create_default_context
        ssl.create_default_context = lambda *a, **k: ssl._create_unverified_context()
        try:
            # Every measured request, including IP and geography, traverses the
            # one exact configured proxy. Times are from the real local sockets.
            result = await outbound.test_proxy_record(records[0], timeout=3)
            assert result["ok"] and result["exit_ip"] == "203.0.113.7"
            assert result["exit_location"] == "Frankfurt, Germany"
            assert {line.split()[1] for line in logs[records[0].id]} == {
                b"cloudflare.com:443", b"google.com:443",
                b"api.ipify.org:443", b"ipapi.co:443",
            }
            assert {host for host, _line in target_log} >= {
                "cloudflare.com", "google.com", "api.ipify.org", "ipapi.co",
            }
            geo_failure = True
            geo_unavailable = await outbound.test_proxy_record(records[1], timeout=3)
            assert geo_unavailable["ok"] and geo_unavailable["exit_ip"] == "203.0.113.7"
            assert geo_unavailable["exit_location"] == ""
            geo_failure = False

            # A complete country scan measures every record before publishing
            # one deterministic preferred ID. It does not use source health.
            await main._scan_proxy_countries({"DE"})
            assert set(main.PROXY_TEST_RESULTS) == {record.id for record in records}
            selected = main.PREFERRED_PROXY_BY_COUNTRY["DE"]
            assert selected == "de-1", {
                key: value["score"] for key, value in main.PROXY_TEST_RESULTS.items()
            }
            assert all(result["sample_count"] >= 1 for result in main.PROXY_TEST_RESULTS.values())
            assert all(result["exit_ip"] == "203.0.113.7" for result in main.PROXY_TEST_RESULTS.values())
            # A later complete test can change the preference for *future*
            # sessions, while existing sessions keep their opened socket.
            delays.update({"de-1": 0.050, "de-2": 0.001, "de-3": 0.028})
            await main._scan_proxy_countries({"DE"})
            assert main.PREFERRED_PROXY_BY_COUNTRY["DE"] == "de-2"
            # Country preference is durable state with no endpoint material.
            await main.save_state(strict=True)
            main.PREFERRED_PROXY_BY_COUNTRY.clear()
            await main.load_state()
            assert main.PREFERRED_PROXY_BY_COUNTRY["DE"] == "de-2"
            assert (await main.current_preferred_proxy_by_country()).get("DE") == "de-2"

            # Country-preferred Multi-Location resolves one current exact ID
            # for each new VLESS session, never a proxy list or another country.
            fr = repo.Record("fr-1", "http://127.0.0.1:1", "http", "France", "FR", "🇫🇷", 1)
            repo._records[fr.id] = fr
            healthy = dict(main.PROXY_TEST_RESULTS["de-1"])
            healthy.update({"proxy_id": "fr-1", "country": "France"})
            main.PROXY_TEST_RESULTS["fr-1"] = healthy
            main.PREFERRED_PROXY_BY_COUNTRY["FR"] = "fr-1"
            catalog = await main.proxy_catalog()
            assert len(catalog["proxies"]) == 4  # authenticated admin inventory
            assert len(catalog["country_options"]) == 2
            de_option = next(row for row in catalog["country_options"] if row["code"] == "DE")
            assert de_option["available"] and "proxy_id" not in de_option
            assert [
                row["id"] for row in catalog["proxies"]
                if row["country_code"] == "DE" and row["preferred"]
            ] == ["de-2"]
            sub_id, _sub = await main.create_sub_group(name="performance-route")
            uid, _link = await main.make_link(label="performance", sub_id=sub_id)
            group = await main.validate_multi_location({
                "enabled": True, "selection_mode": "country_preferred",
                "locations": [{"id": "loc-de", "code": "DE"}, {"id": "loc-fr", "code": "FR"}],
            })
            async with main.SUBS_LOCK:
                main.SUBS[sub_id]["multi_location"] = group
            assert (await main.resolve_exit_selection(main.LINKS[uid], "loc-de"))["proxy_id"] == "de-2"
            original_probe = outbound.test_proxy_record
            failed_ids = []
            async def failed_probe(record, timeout=10.0):
                failed_ids.append(record.id)
                return {
                    "proxy_id": record.id, "ok": False,
                    "checks": [
                        {"target": "https://cloudflare.com", "ok": False, "error": "TimeoutError"},
                        {"target": "https://google.com", "ok": False, "error": "TimeoutError"},
                    ],
                }
            outbound.test_proxy_record = failed_probe
            try:
                await main._scan_proxy_countries({"DE"})
            finally:
                outbound.test_proxy_record = original_probe
            assert set(failed_ids) == {"de-1", "de-2", "de-3"}
            assert "DE" not in main.PREFERRED_PROXY_BY_COUNTRY
            try:
                await main.resolve_exit_selection(main.LINKS[uid], "loc-de")
                raise AssertionError("all-failed country must fail closed")
            except outbound.ProxyUnavailableError:
                pass
            assert (await main.resolve_exit_selection(main.LINKS[uid], "loc-fr"))["proxy_id"] == "fr-1"

            # Concurrent Test-all requests share one bounded scan worker.
            calls = 0
            original_scan = main._scan_proxy_countries
            async def coalesced_scan(_codes):
                nonlocal calls
                calls += 1
                await asyncio.sleep(0.02)
            main._scan_proxy_countries = coalesced_scan
            try:
                started = await asyncio.gather(*(
                    main.schedule_proxy_performance_scan({"DE"}) for _ in range(10)
                ))
                assert sum(bool(item) for item in started) == 1
                await main.PROXY_PERFORMANCE_SCAN_TASK
                assert calls == 1
            finally:
                main._scan_proxy_countries = original_scan

            # Scoring makes reliability meaningful before small latency gains.
            reliable = {"overall_status": "healthy", "sample_count": 100, "success_count": 100, "checks": [
                {"target": "https://cloudflare.com", "ok": True, "total_ms": 100, "connect_ms": 10, "request_ms": 70},
                {"target": "https://google.com", "ok": True, "total_ms": 100, "connect_ms": 10, "request_ms": 70},
            ]}
            unstable = {**reliable, "sample_count": 100, "success_count": 85, "checks": [
                {"target": "https://cloudflare.com", "ok": True, "total_ms": 20, "connect_ms": 2, "request_ms": 15},
                {"target": "https://google.com", "ok": True, "total_ms": 20, "connect_ms": 2, "request_ms": 15},
            ]}
            assert proxy_performance.performance_score(reliable) > proxy_performance.performance_score(unstable)
        finally:
            ssl.create_default_context = original_context
            for server in [tls_server, *proxies]:
                server.close()
                await server.wait_closed()

    print("proxy performance: exact probes=OK all-country scan=OK preferred=OK country-only routing=OK no-fallback=OK")


asyncio.run(run())