#!/usr/bin/env python3
"""Isolated Psiphon Core supervisor contracts.

The test uses a local notice-emitting stand-in, not a Psiphon-compatible
implementation. It validates Lumen's process lifecycle and secret boundary
without claiming a live Psiphon tunnel. A real Core binary plus an authorized
server-entry configuration is required for production end-to-end validation.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psiphon.config import PsiphonConfigError, PsiphonSettings, prepare_runtime_config
from psiphon.manager import PsiphonSessionManager
from psiphon.models import DISABLED, FAILED, HEALTHY, ManagedUpstream, TunnelHealth


async def eventually(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.025)
    raise AssertionError("condition was not reached before timeout")


def write_mock_core(directory: Path) -> tuple[Path, Path]:
    log_path = directory / "core-lifecycle.log"
    program = directory / "official-core-test-double"
    program.write_text(
        "#!" + sys.executable + "\n"
        + textwrap.dedent(
            """\
            import json
            import os
            import signal
            import sys
            import time

            path = os.environ["PSIPHON_TEST_LOG"]
            with open(path, "a", encoding="utf-8") as handle:
                handle.write("start\\n")
                handle.flush()

            def stop(_signum, _frame):
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write("stop\\n")
                    handle.flush()
                raise SystemExit(0)

            signal.signal(signal.SIGTERM, stop)
            print(json.dumps({"noticeType": "ListeningSocksProxyPort", "data": {"port": 19080}}), flush=True)
            print(json.dumps({"noticeType": "ConnectedServerRegion", "data": {"serverRegion": "de"}}), flush=True)
            print(json.dumps({"noticeType": "Tunnels", "data": {"count": 1}}), flush=True)
            while True:
                time.sleep(0.05)
            """
        ),
        encoding="utf-8",
    )
    program.chmod(0o700)
    return program, log_path


async def healthy_probe(host: str, port: int, timeout: float) -> TunnelHealth:
    assert host == "127.0.0.1" and port == 19080 and timeout > 0
    return TunnelHealth(
        True,
        latency_ms=7.25,
        exit_ip="198.51.100.42",
        exit_country="DE",
        exit_region="Berlin",
    )


async def test_disabled(tmp: Path) -> None:
    manager = PsiphonSessionManager(
        PsiphonSettings(False, None, None, tmp / "disabled"),
    )
    await manager.start()
    assert manager.status()["state"] == DISABLED
    assert manager.local_socks_endpoint() is None
    await manager.stop()


async def test_lifecycle_rotation_and_redaction(tmp: Path) -> None:
    binary, log_path = write_mock_core(tmp)
    source_config = tmp / "client.json"
    source_secret = "server-entry-must-never-persist-or-appear-in-status"
    source_config.write_text(
        json.dumps({"TargetServerEntry": source_secret, "LocalSocksProxyPort": 1080}),
        encoding="utf-8",
    )
    retests: list[float] = []

    async def retest() -> None:
        retests.append(time.monotonic())

    async def select() -> ManagedUpstream:
        return ManagedUpstream(
            "stable-proxy-id",
            "socks5://user:password@127.0.0.1:1080",
        )

    settings = PsiphonSettings(
        enabled=True,
        console_client_path=binary,
        config_path=source_config,
        data_dir=tmp / "manager",
        runtime_dir=tmp / "ephemeral-runtime",
        session_max_age_seconds=1,
        healthcheck_interval_seconds=60,
        connect_timeout_seconds=2,
        rotation_grace_seconds=1,
        max_reconnect_attempts=2,
        reconnect_backoff_seconds=1,
        use_managed_upstream_proxy=True,
    )
    manager = PsiphonSessionManager(
        settings,
        retest_proxies=retest,
        select_managed_upstream=select,
        health_probe=healthy_probe,
    )
    os.environ["PSIPHON_TEST_LOG"] = str(log_path)
    try:
        await manager.start()
        await eventually(lambda: manager.status()["state"] == "ACTIVE")
        first_runtime = manager._runtime_config_path
        assert first_runtime and first_runtime.exists()
        runtime_text = first_runtime.read_text(encoding="utf-8")
        runtime = json.loads(runtime_text)
        assert runtime["TunnelPoolSize"] == 1
        assert runtime["DisableLocalHTTPProxy"] is True
        assert runtime["LocalSocksProxyPort"] == 0
        assert runtime["UpstreamProxyURL"].startswith("socks5://")
        status = manager.status()
        assert status["available"] is True
        assert status["selected_location"] == "DE"
        assert status["tcp_tunnel_health"] == "PASS"
        assert status["udp_tunnel_health"] == "NOT_SUPPORTED"
        assert status["core_available"] is True
        assert status["psiphon_ws_status"] == "AVAILABLE"
        assert status["active_psiphon_ws_sessions"] == 0
        assert source_secret not in json.dumps(status)
        assert "password@" not in json.dumps(status)

        # The new VLESS/WS endpoint can only lease the active Core generation.
        # Its listener is always loopback and the active-session count is safe
        # operational metadata, never a Core credential or socket address.
        lease = await manager.acquire_vless_backend()
        assert lease is not None
        assert lease.host == "127.0.0.1" and lease.port == 19080
        assert manager.status()["active_psiphon_ws_sessions"] == 1
        await manager.release_vless_backend(lease)
        assert manager.status()["active_psiphon_ws_sessions"] == 0

        # The 1-second test setting forces the same graceful rotation sequence
        # used by the production-capped 30-minute setting.
        await eventually(
            lambda: manager.status()["rotation_count"] >= 1
            and manager.status()["state"] == "ACTIVE",
            timeout=6,
        )
        assert len(retests) >= 2, "rotation must re-test candidates"
        assert log_path.read_text(encoding="utf-8").count("start\n") >= 2
        assert log_path.read_text(encoding="utf-8").count("stop\n") >= 1

        persisted = (settings.data_dir / "session-state.json").read_text(encoding="utf-8")
        assert source_secret not in persisted
        assert "password@" not in persisted
        assert "UpstreamProxyURL" not in persisted
        await manager.stop()
        assert manager.status()["state"] != "ACTIVE"
        assert await manager.acquire_vless_backend() is None
        assert manager._runtime_config_path is None
        assert settings.runtime_dir is not None
        assert not list(settings.runtime_dir.glob("core-*.json"))
    finally:
        os.environ.pop("PSIPHON_TEST_LOG", None)
        await manager.stop()


def test_config_rejections(tmp: Path) -> None:
    binary, _log = write_mock_core(tmp)
    unsafe = tmp / "unsafe.json"
    unsafe.write_text(json.dumps({"EnableLightProxyFallback": True}), encoding="utf-8")
    settings = PsiphonSettings(True, binary, unsafe, tmp / "unsafe-runtime")
    try:
        prepare_runtime_config(settings)
    except PsiphonConfigError as exc:
        assert str(exc) == "light_proxy_fallback_disallowed"
    else:
        raise AssertionError("light-proxy fallback must be rejected")

    packet = tmp / "packet.json"
    packet.write_text(json.dumps({"PacketTunnelTunFileDescriptor": 4}), encoding="utf-8")
    settings = PsiphonSettings(True, binary, packet, tmp / "packet-runtime")
    try:
        prepare_runtime_config(settings)
    except PsiphonConfigError as exc:
        assert str(exc) == "packet_tunnel_not_supported"
    else:
        raise AssertionError("packet-tunnel config must be rejected")


async def test_failure_is_isolated(tmp: Path) -> None:
    config = tmp / "bad-config.json"
    config.write_text("{", encoding="utf-8")
    binary, _log = write_mock_core(tmp)
    manager = PsiphonSessionManager(
        PsiphonSettings(
            True, binary, config, tmp / "bad-manager",
            connect_timeout_seconds=1, max_reconnect_attempts=1,
        ),
        health_probe=healthy_probe,
    )
    await manager.start()
    await eventually(lambda: manager.status()["state"] == "UNAVAILABLE")
    assert manager.local_socks_endpoint() is None
    await manager.stop()


async def test_catalog_retest_isolated(tmp: Path) -> None:
    """Psiphon candidate testing cannot mutate VLESS preference state."""
    previous_data_dir = os.environ.get("DATA_DIR")
    os.environ["DATA_DIR"] = str(tmp / "main-state")
    try:
        import main

        records = (
            SimpleNamespace(id="slow", country="Germany", code="DE", url="http://slow.test:8080"),
            SimpleNamespace(id="fast", country="Germany", code="DE", url="socks5://fast.test:1080"),
        )
        called: list[str] = []
        original_records = main.proxy_repository.records_for_country
        original_probe = main.outbound.test_proxy_record
        normal_results = dict(main.PROXY_TEST_RESULTS)
        normal_preferred = dict(main.PREFERRED_PROXY_BY_COUNTRY)
        psiphon_results = dict(main.PSIPHON_PROXY_RETEST_RESULTS)
        try:
            main.proxy_repository.records_for_country = lambda _code=None: records

            async def fake_probe(record):
                called.append(record.id)
                latency = 30 if record.id == "fast" else 90
                return {
                    "proxy_id": record.id,
                    "ok": True,
                    "checks": [
                        {
                            "target": target,
                            "ok": True,
                            "status": 200,
                            "connect_ms": latency / 3,
                            "handshake_ms": latency / 3,
                            "request_ms": latency / 3,
                            "total_ms": latency,
                        }
                        for target in sorted(main.proxy_performance.REQUIRED_TARGETS)
                    ],
                    "exit_ip": "198.51.100.8",
                    "exit_country_code": "DE",
                    "exit_location": "Test",
                }

            main.outbound.test_proxy_record = fake_probe
            main.PROXY_TEST_RESULTS.clear()
            main.PROXY_TEST_RESULTS["normal-only"] = {"untouched": True}
            main.PREFERRED_PROXY_BY_COUNTRY.clear()
            main.PREFERRED_PROXY_BY_COUNTRY["FR"] = "normal-fr"
            await main._psiphon_retest_all_proxies()
            assert set(called) == {"slow", "fast"}
            assert main.PROXY_TEST_RESULTS == {"normal-only": {"untouched": True}}
            assert main.PREFERRED_PROXY_BY_COUNTRY == {"FR": "normal-fr"}
            selected = await main._psiphon_select_managed_upstream()
            assert selected is not None and selected.proxy_id == "fast"
        finally:
            main.proxy_repository.records_for_country = original_records
            main.outbound.test_proxy_record = original_probe
            main.PROXY_TEST_RESULTS.clear()
            main.PROXY_TEST_RESULTS.update(normal_results)
            main.PREFERRED_PROXY_BY_COUNTRY.clear()
            main.PREFERRED_PROXY_BY_COUNTRY.update(normal_preferred)
            main.PSIPHON_PROXY_RETEST_RESULTS.clear()
            main.PSIPHON_PROXY_RETEST_RESULTS.update(psiphon_results)
    finally:
        if previous_data_dir is None:
            os.environ.pop("DATA_DIR", None)
        else:
            os.environ["DATA_DIR"] = previous_data_dir


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="lumen-psiphon-test-") as name:
        tmp = Path(name)
        await test_disabled(tmp)
        test_config_rejections(tmp)
        await test_lifecycle_rotation_and_redaction(tmp)
        await test_failure_is_isolated(tmp)
        await test_catalog_retest_isolated(tmp)
    source = (ROOT / "relay_vless.py").read_text(encoding="utf-8")
    assert "psiphon" not in source.lower(), "Psiphon must not enter the WS relay"
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    page_source = (ROOT / "pages.py").read_text(encoding="utf-8")
    assert '@app.get("/api/psiphon/status")' in main_source
    assert "psiphon-status" in page_source
    assert "PSIPHON_ENABLED" in (ROOT / "README.md").read_text(encoding="utf-8")
    print("psiphon manager: isolated lifecycle=OK rotation=OK leasing=OK redaction=OK UDP=NOT_SUPPORTED")


if __name__ == "__main__":
    asyncio.run(main())