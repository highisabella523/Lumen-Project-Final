"""Isolated supervisor for an operator-provided Psiphon ConsoleClient."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import PsiphonConfigError, PsiphonSettings, prepare_runtime_config
from .health import probe_tunnel
from .models import (
    CONNECTED,
    CONNECTING,
    DEGRADED,
    DISABLED,
    FAILED,
    HEALTHY,
    RECONNECTING,
    ROTATING,
    STARTING,
    STOPPED,
    ManagedUpstream,
    PsiphonBackendLease,
    SessionStatus,
    TunnelHealth,
    bounded_float,
    safe_region,
    utc_now,
    utc_now_iso,
)
from .state import OperationalStateStore

_NOTICE_LINE_LIMIT = 16 * 1024
_SAFE_FAILURES = frozenset(
    {
        "core_binary_missing",
        "core_binary_not_executable",
        "core_config_missing",
        "core_config_invalid",
        "light_proxy_fallback_disallowed",
        "tunnels_disabled",
        "packet_tunnel_not_supported",
        "egress_region_invalid",
        "managed_upstream_unavailable",
        "managed_upstream_invalid",
        "core_spawn_failed",
        "core_exited",
        "core_notice_error",
        "core_notice_too_large",
        "tunnel_ready_timeout",
        "tunnel_lost",
        "https_probe_failed",
        "exit_identity_unavailable",
        "proxy_retest_failed",
        "shutdown",
    }
)

ProxyRetest = Callable[[], Awaitable[None] | None]
UpstreamSelector = Callable[[], Awaitable[ManagedUpstream | None] | ManagedUpstream | None]
HealthProbe = Callable[[str, int, float], Awaitable[TunnelHealth]]


class PsiphonSessionManager:
    """Owns one official Core process and one loopback-only SOCKS endpoint.

    The manager has no import from ``relay_vless`` and no public forwarding
    listener.  This is deliberate: Psiphon ConsoleClient is an official client
    component, not a generic public subscription protocol or a replacement for
    the existing VLESS relay.
    """

    def __init__(
        self,
        settings: PsiphonSettings,
        *,
        retest_proxies: ProxyRetest | None = None,
        select_managed_upstream: UpstreamSelector | None = None,
        health_probe: HealthProbe = probe_tunnel,
        logger: Any | None = None,
    ):
        self.settings = settings
        self._retest_proxies = retest_proxies
        self._select_managed_upstream = select_managed_upstream
        self._health_probe = health_probe
        self._logger = logger
        self._store = OperationalStateStore(settings.data_dir)
        self._status = SessionStatus(enabled=settings.enabled)
        self._process: asyncio.subprocess.Process | None = None
        self._runtime_config_path: Path | None = None
        self._notice_tasks: set[asyncio.Task] = set()
        self._supervisor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._socks_port: int | None = None
        self._active_tunnels = 0
        self._started_monotonic = 0.0
        self._last_health_monotonic = 0.0
        self._notice_failure = ""
        self._stabilized = False
        self._generation = 0
        self._accepting_vless_sessions = False
        self._leases: set[str] = set()
        self._lease_lock = asyncio.Lock()

    def set_callbacks(
        self,
        *,
        retest_proxies: ProxyRetest | None = None,
        select_managed_upstream: UpstreamSelector | None = None,
    ) -> None:
        """Wire control-plane callbacks after the host application is loaded."""
        self._retest_proxies = retest_proxies
        self._select_managed_upstream = select_managed_upstream

    def status(self) -> dict[str, Any]:
        # Core state alone is not enough to advertise the VLESS/WS route:
        # rotation disables new backend leases before the old process exits.
        # Report that short transition as unavailable rather than publishing a
        # profile that will immediately fail.
        public = self._status.public()
        available = self.vless_transport_available()
        process_running = bool(self._process and self._process.returncode is None)
        unavailable_errors = {"core_binary_missing", "core_binary_not_executable", "core_config_missing", "core_config_invalid"}
        if not self.settings.enabled:
            public_state = "DISABLED"
        elif self._status.last_error in unavailable_errors:
            public_state = "UNAVAILABLE"
        elif available:
            public_state = "ACTIVE"
        elif self._status.state == STOPPED:
            public_state = "STOPPED"
        elif self._status.state == FAILED:
            public_state = "FAILED"
        else:
            public_state = "STARTING"
        public.update({
            "state": public_state,
            "core_available": process_running,
            "available": available,
            "psiphon_ws_status": "AVAILABLE" if available else "UNAVAILABLE",
        })
        return public

    def local_socks_endpoint(self) -> tuple[str, int] | None:
        """Internal-only access for future trusted in-process integrations."""
        if self._status.state != HEALTHY or not self._socks_port:
            return None
        return "127.0.0.1", self._socks_port

    def vless_transport_available(self) -> bool:
        """Whether the isolated VLESS/WS Psiphon endpoint may accept a session."""
        return bool(
            self._accepting_vless_sessions
            and self._status.state == HEALTHY
            and self._socks_port
        )

    async def acquire_vless_backend(self) -> PsiphonBackendLease | None:
        """Atomically lease the current loopback Core endpoint for one session."""
        async with self._lease_lock:
            if not self.vless_transport_available():
                return None
            lease_id = uuid4().hex
            self._leases.add(lease_id)
            self._status.active_psiphon_ws_sessions = len(self._leases)
            return PsiphonBackendLease(
                lease_id=lease_id,
                generation=self._generation,
                host="127.0.0.1",
                port=int(self._socks_port or 0),
            )

    async def release_vless_backend(self, lease: PsiphonBackendLease | None) -> None:
        if lease is None:
            return
        async with self._lease_lock:
            self._leases.discard(lease.lease_id)
            self._status.active_psiphon_ws_sessions = len(self._leases)

    async def start(self) -> None:
        """Schedule optional Core supervision without delaying Lumen startup."""
        if not self.settings.enabled:
            self._set_state(DISABLED)
            return
        async with self._lifecycle_lock:
            if self._supervisor_task and not self._supervisor_task.done():
                return
            try:
                self.settings.validate_startup()
                self._status.core_available = True
                persisted = await self._store.load()
                self._restore_persisted(persisted)
            except PsiphonConfigError as exc:
                self._status.core_available = False
                await self._fail(str(exc))
                return
            self._stop_event.clear()
            self._supervisor_task = asyncio.create_task(
                self._supervise(), name="psiphon-session-supervisor",
            )
            self._log("psiphon.session.start", state=STARTING)

    async def stop(self) -> None:
        """Stop the supervisor and its child Core process with bounded waits."""
        self._stop_event.set()
        async with self._lifecycle_lock:
            supervisor = self._supervisor_task
            self._supervisor_task = None
        if supervisor and supervisor is not asyncio.current_task():
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await self._stop_active_process()
        if self.settings.enabled:
            self._set_state(STOPPED, reason="shutdown")
            await self._persist()
        else:
            self._set_state(DISABLED)
        self._log("psiphon.session.stop", state=self._status.state)

    async def _supervise(self) -> None:
        attempts = 0
        rotate_pending = False
        retest_before_start = True
        try:
            while not self._stop_event.is_set():
                started = await self._start_session(
                    retest_proxies=retest_before_start,
                    rotating=rotate_pending,
                )
                retest_before_start = False
                if not started:
                    attempts += 1
                    self._status.restart_count = attempts
                    if attempts >= self.settings.max_reconnect_attempts:
                        await self._fail(self._status.last_error or "core_spawn_failed")
                        return
                    self._set_state(RECONNECTING, reason=self._status.last_error)
                    await self._persist()
                    delay = min(
                        60,
                        self.settings.reconnect_backoff_seconds * (2 ** (attempts - 1)),
                    )
                    self._log("psiphon.session.reconnecting", attempt=attempts, delay=delay)
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        continue
                    break

                if rotate_pending:
                    attempts = 0
                    self._status.restart_count = 0
                    self._status.rotation_count += 1
                    self._status.last_rotation_at = utc_now_iso()
                    await self._persist()
                rotate_pending = False

                reason = await self._monitor_active_session()
                if self._stop_event.is_set():
                    break
                rotate_pending = reason == "expired"
                retest_before_start = rotate_pending
                if rotate_pending:
                    self._set_state(ROTATING)
                    self._log("psiphon.session.rotate", reason="max_age")
                else:
                    if self._stabilized:
                        attempts = 0
                    attempts += 1
                    self._status.restart_count = attempts
                    if attempts >= self.settings.max_reconnect_attempts:
                        await self._stop_active_process()
                        await self._fail(reason)
                        return
                    self._set_state(RECONNECTING, reason=reason)
                    self._log("psiphon.session.failed", reason=reason)
                await self._stop_active_process()
                if not rotate_pending:
                    await self._persist()
                    delay = min(
                        60,
                        self.settings.reconnect_backoff_seconds * (2 ** (attempts - 1)),
                    )
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        continue
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._fail("core_spawn_failed")
        finally:
            if self._stop_event.is_set():
                await self._stop_active_process()

    async def _start_session(self, *, retest_proxies: bool, rotating: bool) -> bool:
        self._set_state(ROTATING if rotating else STARTING)
        self._notice_failure = ""
        self._ready_event.clear()
        self._socks_port = None
        self._active_tunnels = 0
        self._stabilized = False
        self._accepting_vless_sessions = False

        if retest_proxies and self.settings.use_managed_upstream_proxy:
            self._log("psiphon.proxy_test.start")
            try:
                await self._call(self._retest_proxies)
                self._status.last_proxy_retest_at = utc_now_iso()
                self._log("psiphon.proxy_test.complete", result="ok")
            except asyncio.CancelledError:
                raise
            except Exception:
                # A managed upstream session must not start from stale data.
                if self.settings.use_managed_upstream_proxy:
                    await self._fail("proxy_retest_failed")
                    return False
                self._status.last_proxy_retest_at = utc_now_iso()
                self._log("psiphon.proxy_test.complete", result="failed")

        upstream: ManagedUpstream | None = None
        if self.settings.use_managed_upstream_proxy:
            try:
                candidate = await self._call(self._select_managed_upstream)
            except asyncio.CancelledError:
                raise
            except Exception:
                candidate = None
            if not isinstance(candidate, ManagedUpstream):
                await self._fail("managed_upstream_unavailable")
                return False
            upstream = candidate

        try:
            runtime_config = await asyncio.to_thread(
                prepare_runtime_config, self.settings, upstream,
            )
            self._runtime_config_path = runtime_config
            self.settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            core_data_dir = self.settings.data_dir / "core-data"
            core_data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(self.settings.data_dir, 0o700)
                os.chmod(core_data_dir, 0o700)
            except OSError:
                pass
            assert self.settings.console_client_path is not None
            working_dir = (
                self.settings.config_path.parent
                if self.settings.config_path is not None
                else (self.settings.runtime_dir or self.settings.data_dir)
            )
            self._process = await asyncio.create_subprocess_exec(
                str(self.settings.console_client_path),
                "-config",
                str(runtime_config),
                "-dataRootDirectory",
                str(core_data_dir),
                cwd=str(working_dir),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_NOTICE_LINE_LIMIT,
                start_new_session=(os.name != "nt"),
            )
        except PsiphonConfigError as exc:
            await self._fail(str(exc))
            await self._stop_active_process()
            return False
        except (OSError, ValueError):
            await self._fail("core_spawn_failed")
            await self._stop_active_process()
            return False

        self._status.session_id = uuid4().hex
        self._status.session_started_at = utc_now_iso()
        self._status.session_expires_at = (
            utc_now().timestamp() + self.settings.session_max_age_seconds
        )
        self._status.session_expires_at = _iso_from_timestamp(self._status.session_expires_at)
        self._started_monotonic = time.monotonic()
        self._set_state(CONNECTING)
        self._start_notice_readers()
        if not await self._wait_for_ready():
            await self._fail(self._notice_failure or "tunnel_ready_timeout")
            await self._stop_active_process()
            return False

        self._set_state(CONNECTED)
        health = await self._run_health_check()
        if not health.tcp_ok:
            await self._fail(health.reason or "https_probe_failed")
            await self._stop_active_process()
            return False

        # HTTPS traffic has traversed the tunnel. Exit identity is useful
        # telemetry, but a geo/IP lookup failure is not a fabricated failure.
        self._set_state(HEALTHY, reason=health.reason)
        self._status.tunnel_setup_ms = bounded_float(
            (time.monotonic() - self._started_monotonic) * 1000.0,
        )
        self._status.exit_ip = health.exit_ip
        self._status.exit_country = health.exit_country
        self._status.exit_region = health.exit_region
        self._status.last_successful_health_check_at = utc_now_iso()
        self._last_health_monotonic = time.monotonic()
        self._generation += 1
        self._accepting_vless_sessions = True
        await self._persist()
        self._log(
            "psiphon.session.healthy",
            latency_ms=round(self._status.tunnel_setup_ms, 2),
            location=self._status.selected_location or "UNAVAILABLE",
        )
        return True

    async def _monitor_active_session(self) -> str:
        while not self._stop_event.is_set():
            process = self._process
            if process is None:
                return "core_exited"
            if process.returncode is not None:
                self._status.last_exit_code = process.returncode
                return "core_exited"
            if self._notice_failure:
                return self._notice_failure
            if self._active_tunnels <= 0:
                return "tunnel_lost"
            age = time.monotonic() - self._started_monotonic
            if age >= self.settings.session_max_age_seconds:
                return "expired"
            until_health = max(
                0.0,
                self.settings.healthcheck_interval_seconds
                - (time.monotonic() - self._last_health_monotonic),
            )
            until_rotation = max(0.0, self.settings.session_max_age_seconds - age)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=min(until_health, until_rotation, 5.0),
                )
                return "shutdown"
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - self._last_health_monotonic >= self.settings.healthcheck_interval_seconds:
                health = await self._run_health_check()
                if not health.tcp_ok:
                    return health.reason or "https_probe_failed"
                self._status.exit_ip = health.exit_ip or self._status.exit_ip
                self._status.exit_country = health.exit_country or self._status.exit_country
                self._status.exit_region = health.exit_region or self._status.exit_region
                self._status.last_error = health.reason if health.reason in _SAFE_FAILURES else ""
                self._status.last_successful_health_check_at = utc_now_iso()
                self._last_health_monotonic = time.monotonic()
                self._stabilized = True
                await self._persist()
        return "shutdown"

    async def _run_health_check(self) -> TunnelHealth:
        port = self._socks_port
        if not port:
            return TunnelHealth(False, reason="tunnel_ready_timeout")
        try:
            health = await asyncio.wait_for(
                self._health_probe("127.0.0.1", port, self.settings.connect_timeout_seconds),
                timeout=self.settings.connect_timeout_seconds + 5,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            health = TunnelHealth(False, reason="https_probe_failed")
        self._status.tcp_tunnel_health = "PASS" if health.tcp_ok else "FAIL"
        return health

    def _start_notice_readers(self) -> None:
        assert self._process is not None
        for stream in (self._process.stdout, self._process.stderr):
            if stream is None:
                continue
            task = asyncio.create_task(
                self._consume_notices(stream), name="psiphon-core-notices",
            )
            self._notice_tasks.add(task)
            task.add_done_callback(self._notice_tasks.discard)

    async def _consume_notices(self, stream: asyncio.StreamReader) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    line = await stream.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self._notice_failure = "core_notice_too_large"
                    self._ready_event.set()
                    return
                if not line:
                    return
                if len(line) > _NOTICE_LINE_LIMIT:
                    self._notice_failure = "core_notice_too_large"
                    self._ready_event.set()
                    return
                self._handle_notice(line)
        except asyncio.CancelledError:
            raise

    def _handle_notice(self, line: bytes) -> None:
        try:
            notice = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(notice, dict):
            return
        notice_type = str(notice.get("noticeType") or "")
        data = notice.get("data") if isinstance(notice.get("data"), dict) else {}
        if notice_type == "ListeningSocksProxyPort":
            try:
                port = int(data.get("port"))
            except (TypeError, ValueError):
                return
            if 1 <= port <= 65535:
                self._socks_port = port
        elif notice_type == "Tunnels":
            try:
                self._active_tunnels = max(0, int(data.get("count")))
            except (TypeError, ValueError):
                return
        elif notice_type == "ConnectedServerRegion":
            self._status.selected_location = safe_region(data.get("serverRegion"))
        elif notice_type == "Error":
            self._notice_failure = "core_notice_error"
        if self._socks_port and self._active_tunnels > 0:
            self._ready_event.set()

    async def _wait_for_ready(self) -> bool:
        process = self._process
        if process is None:
            return False
        ready_task = asyncio.create_task(self._ready_event.wait())
        exit_task = asyncio.create_task(process.wait())
        try:
            done, _pending = await asyncio.wait(
                {ready_task, exit_task},
                timeout=self.settings.connect_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_task in done and self._ready_event.is_set() and not self._notice_failure:
                return True
            if exit_task in done:
                self._status.last_exit_code = process.returncode
                self._notice_failure = self._notice_failure or "core_exited"
            return False
        finally:
            for task in (ready_task, exit_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(ready_task, exit_task, return_exceptions=True)

    async def _stop_active_process(self) -> None:
        # The state transition occurs before the Core process is signaled, so
        # new VLESS/WS requests can never acquire an expiring session.
        self._accepting_vless_sessions = False
        process, self._process = self._process, None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self.settings.rotation_grace_seconds)
            except asyncio.TimeoutError:
                if os.name != "nt":
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                else:
                    process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
        if process and process.returncode is not None:
            self._status.last_exit_code = process.returncode
        tasks = tuple(self._notice_tasks)
        self._notice_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        runtime_config, self._runtime_config_path = self._runtime_config_path, None
        if runtime_config:
            try:
                runtime_config.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                self._log("psiphon.session.failed", reason="runtime_config_cleanup_failed")
        self._socks_port = None
        self._active_tunnels = 0
        self._ready_event.clear()
        self._status.session_id = ""
        self._status.session_started_at = ""
        self._status.session_expires_at = ""
        self._status.exit_ip = ""
        self._status.exit_country = ""
        self._status.exit_region = ""
        self._status.tcp_tunnel_health = "UNKNOWN"

    async def _fail(self, reason: str) -> None:
        self._set_state(FAILED, reason=reason)
        await self._persist()
        self._log("psiphon.session.failed", reason=self._status.last_error)

    def _set_state(self, state: str, *, reason: str = "") -> None:
        self._status.state = state
        self._status.last_error = reason if reason in _SAFE_FAILURES else ""

    async def _persist(self) -> None:
        try:
            await self._store.save(
                {
                    "last_successful_location": self._status.selected_location,
                    "last_successful_setup_ms": self._status.tunnel_setup_ms,
                    "last_rotation_at": self._status.last_rotation_at,
                    "rotation_count": self._status.rotation_count,
                    "last_proxy_retest_at": self._status.last_proxy_retest_at,
                    "last_successful_health_check_at": self._status.last_successful_health_check_at,
                    "last_state": self._status.state,
                }
            )
        except Exception:
            self._log("psiphon.session.failed", reason="state_persist_failed")

    def _restore_persisted(self, data: dict[str, Any]) -> None:
        self._status.selected_location = safe_region(data.get("last_successful_location"))
        self._status.tunnel_setup_ms = bounded_float(data.get("last_successful_setup_ms"))
        self._status.last_rotation_at = str(data.get("last_rotation_at") or "")
        self._status.rotation_count = max(0, int(data.get("rotation_count") or 0))
        self._status.last_proxy_retest_at = str(data.get("last_proxy_retest_at") or "")
        self._status.last_successful_health_check_at = str(
            data.get("last_successful_health_check_at") or "",
        )

    async def _call(self, callback: Callable | None):
        if callback is None:
            return None
        value = callback()
        if inspect.isawaitable(value):
            return await value
        return value

    def _log(self, event: str, **fields: object) -> None:
        if self._logger is None:
            return
        safe = {"session_id": self._status.session_id[:12] or "none"}
        for key, value in fields.items():
            text = str(value)
            if key in {"state", "reason", "result", "location"}:
                safe[key] = text[:80]
            elif key in {"attempt", "delay", "latency_ms"}:
                safe[key] = text[:32]
        suffix = " ".join(f"{key}={value}" for key, value in safe.items())
        self._logger.info("%s %s", event, suffix)


def _iso_from_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()