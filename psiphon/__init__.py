"""Optional, isolated Psiphon Tunnel Core integration.

This package deliberately has no dependency on the VLESS/WebSocket relay.  It
only supervises an operator-provided official Psiphon ConsoleClient process
and its loopback-only SOCKS endpoint.
"""

from .config import PsiphonConfigError, PsiphonSettings
from .manager import PsiphonSessionManager

__all__ = ("PsiphonConfigError", "PsiphonSettings", "PsiphonSessionManager")