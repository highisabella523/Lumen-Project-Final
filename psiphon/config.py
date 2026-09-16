"""Environment parsing and short-lived official Core runtime configuration."""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .models import ManagedUpstream

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_CORE_UPSTREAM_SCHEMES = frozenset({"http", "socks4a", "socks5"})


class PsiphonConfigError(ValueError):
    """Safe error categories only; never include config contents or paths."""


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(value, high))


@dataclass(frozen=True)
class PsiphonSettings:
    enabled: bool
    console_client_path: Path | None
    config_path: Path | None
    data_dir: Path
    config_b64: str = ""
    runtime_dir: Path | None = None
    session_max_age_seconds: int = 1800
    healthcheck_interval_seconds: int = 60
    connect_timeout_seconds: int = 45
    rotation_grace_seconds: int = 15
    max_reconnect_attempts: int = 3
    reconnect_backoff_seconds: int = 5
    egress_region: str = ""
    use_managed_upstream_proxy: bool = False
    environment_error: str = ""

    @classmethod
    def from_environment(cls, default_data_dir: Path) -> "PsiphonSettings":
        raw_region = os.environ.get("PSIPHON_EGRESS_REGION", "").strip().upper()
        region = raw_region if not raw_region or _COUNTRY_CODE.fullmatch(raw_region) else ""
        binary = os.environ.get("PSIPHON_CONSOLE_CLIENT_PATH", "").strip()
        config = os.environ.get("PSIPHON_CONFIG_PATH", "").strip()
        runtime_dir = Path(os.environ.get("PSIPHON_DATA_DIR", str(default_data_dir))).expanduser()
        ephemeral_runtime_dir = Path(
            os.environ.get(
                "PSIPHON_RUNTIME_DIR",
                str(Path(tempfile.gettempdir()) / "lumen-psiphon-runtime"),
            )
        ).expanduser()
        return cls(
            enabled=_bool_env("PSIPHON_ENABLED", False),
            console_client_path=Path(binary).expanduser() if binary else None,
            config_path=Path(config).expanduser() if config else None,
            config_b64=os.environ.get("PSIPHON_CONFIG_B64", "").strip(),
            data_dir=runtime_dir,
            runtime_dir=ephemeral_runtime_dir,
            # Thirty minutes is a hard upper bound, not merely a default.
            session_max_age_seconds=_bounded_env_int(
                "PSIPHON_SESSION_MAX_AGE_SECONDS", 1800, 60, 1800,
            ),
            healthcheck_interval_seconds=_bounded_env_int(
                "PSIPHON_HEALTHCHECK_INTERVAL_SECONDS", 60, 10, 900,
            ),
            connect_timeout_seconds=_bounded_env_int(
                "PSIPHON_CONNECT_TIMEOUT_SECONDS", 45, 5, 120,
            ),
            rotation_grace_seconds=_bounded_env_int(
                "PSIPHON_ROTATION_GRACE_SECONDS", 15, 1, 60,
            ),
            max_reconnect_attempts=_bounded_env_int(
                "PSIPHON_MAX_RECONNECT_ATTEMPTS", 3, 1, 8,
            ),
            reconnect_backoff_seconds=_bounded_env_int(
                "PSIPHON_RECONNECT_BACKOFF_SECONDS", 5, 1, 60,
            ),
            egress_region=region,
            use_managed_upstream_proxy=_bool_env(
                "PSIPHON_USE_MANAGED_UPSTREAM_PROXY", False,
            ),
            environment_error="" if not raw_region or region else "egress_region_invalid",
        )

    def validate_startup(self) -> None:
        if not self.enabled:
            return
        if self.environment_error:
            raise PsiphonConfigError(self.environment_error)
        if self.console_client_path is None:
            raise PsiphonConfigError("core_binary_missing")
        if not self.console_client_path.is_file():
            raise PsiphonConfigError("core_binary_missing")
        if not os.access(self.console_client_path, os.X_OK):
            raise PsiphonConfigError("core_binary_not_executable")
        if not self.config_b64 and (self.config_path is None or not self.config_path.is_file()):
            raise PsiphonConfigError("core_config_missing")


def _validate_upstream(upstream: ManagedUpstream | None) -> str:
    if upstream is None or not upstream.url:
        raise PsiphonConfigError("managed_upstream_unavailable")
    try:
        parsed = urlsplit(upstream.url)
    except ValueError as exc:
        raise PsiphonConfigError("managed_upstream_invalid") from exc
    if parsed.scheme.lower() not in _CORE_UPSTREAM_SCHEMES or not parsed.hostname:
        raise PsiphonConfigError("managed_upstream_invalid")
    try:
        if parsed.port is None:
            raise PsiphonConfigError("managed_upstream_invalid")
    except ValueError as exc:
        raise PsiphonConfigError("managed_upstream_invalid") from exc
    return upstream.url


def prepare_runtime_config(
    settings: PsiphonSettings,
    upstream: ManagedUpstream | None = None,
) -> Path:
    """Create a 0600 per-session Core config without changing the source file.

    The source Core config is operator-supplied and may contain server entries
    or other sensitive data. It is neither persisted by Lumen nor exposed in
    status/subscription responses.
    """
    settings.validate_startup()
    try:
        if settings.config_b64:
            encoded = settings.config_b64.encode("ascii")
            if len(encoded) > 4 * 1024 * 1024:
                raise PsiphonConfigError("core_config_invalid")
            config_text = base64.b64decode(encoded, validate=True).decode("utf-8")
        else:
            assert settings.config_path is not None
            config_text = settings.config_path.read_text(encoding="utf-8")
        raw = json.loads(config_text)
    except (OSError, ValueError, TypeError, UnicodeError, binascii.Error) as exc:
        raise PsiphonConfigError("core_config_invalid") from exc
    if not isinstance(raw, dict):
        raise PsiphonConfigError("core_config_invalid")
    if raw.get("EnableLightProxyFallback") or raw.get("EnableLightProxy"):
        # A light proxy is not a Psiphon tunnel and does not preserve the
        # selected Psiphon egress semantics.
        raise PsiphonConfigError("light_proxy_fallback_disallowed")
    if raw.get("DisableTunnels"):
        raise PsiphonConfigError("tunnels_disabled")
    if raw.get("PacketTunnelTunFileDescriptor"):
        # This manager owns the official port-forward mode only; it never
        # pretends that a Railway service has a public packet-tunnel/TUN path.
        raise PsiphonConfigError("packet_tunnel_not_supported")

    runtime = dict(raw)
    # Official Core defaults bind loopback when ListenInterface is empty. Force
    # a loopback-only random SOCKS port and suppress its HTTP listener. Lumen
    # never exposes the Core proxy as a public endpoint.
    runtime.update(
        {
            "ListenInterface": "",
            "UseUnixDomainSockets": False,
            "DisableLocalSocksProxy": False,
            "LocalSocksProxyPort": 0,
            "DisableLocalHTTPProxy": True,
            "LocalHttpProxyPort": 0,
            "TunnelPoolSize": 1,
        }
    )
    if settings.egress_region:
        runtime["EgressRegion"] = settings.egress_region
    if settings.use_managed_upstream_proxy:
        runtime["UpstreamProxyURL"] = _validate_upstream(upstream)

    # The derived config can contain server-entry and upstream credentials. It
    # is runtime-only, unlike the non-secret OperationalStateStore data.
    runtime_dir = settings.runtime_dir or (Path(tempfile.gettempdir()) / "lumen-psiphon-runtime")
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(runtime_dir, 0o700)
    except OSError:
        pass
    # Runtime directories are local to one service process. A preceding
    # abnormal exit may leave an old 0600 derived config behind; remove only
    # files bearing this manager's private prefix before creating a new one.
    for stale in runtime_dir.glob("core-*.json"):
        try:
            stale.unlink()
        except OSError:
            pass
    fd, name = tempfile.mkstemp(prefix="core-", suffix=".json", dir=str(runtime_dir))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(runtime, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise
    return Path(name)