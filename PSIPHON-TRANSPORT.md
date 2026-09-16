# Psiphon Transport — isolated optional Core supervisor

## Scope and safety boundary

This package supervises one operator-provided, official Psiphon Tunnel Core
**ConsoleClient** process. It is independent from the native FastAPI/Uvicorn
VLESS-over-WebSocket relay:

```text
Existing VLESS:
Client → Railway HTTPS/WSS → /ws/{uuid} → VLESS relay → exact outbound → Internet

Optional Psiphon:
Client → Railway HTTPS/WSS → /ws/p-core/cq/{uuid} → isolated VLESS relay
       → loopback SOCKS5 → official ConsoleClient → Psiphon tunnel → Internet
```

There is no call from `relay_vless.py` to the Psiphon package. The Psiphon
manager never changes a connection on `/ws/{uuid}`, its UUID behavior, `loc`
value, Multi-Location mapping, proxy selection, or WebSocket frame. The
additive `/ws/p-core/cq/{uuid}` route uses the same standard VLESS request
parser and account UUID, but has a separate relay backend which can open only
through the managed Core loopback SOCKS endpoint.

## Prerequisites

1. The container build compiles the supplied, vendored official
   `ConsoleClient` entry point from `third_party/psiphon-tunnel-core` using the
   repository's Go 1.26 toolchain declaration and vendored modules. The binary
   is installed at `/usr/local/bin/psiphon-tunnel-core`.
2. Provide an authorized Core JSON configuration either as base64 in the
   Railway secret `PSIPHON_CONFIG_B64`, or through a read-only/persistent-volume
   file at `PSIPHON_CONFIG_PATH`. The supplied upstream source does not include
   production Sponsor/Propagation IDs or authorized server entries.
3. Set `PSIPHON_ENABLED=true` only after the authorized configuration exists.
4. Set `PSIPHON_ENABLED=true` only in staging first.
5. Use a persistent `DATA_DIR`; the manager stores only non-secret operational
   history in `<PSIPHON_DATA_DIR>/session-state.json`.

Lumen does **not** ship a Core binary, generate a server entry, fetch an
unauthorized server list, or expose the Core config file. The upstream project
is GPL-3.0; obtain legal review before bundling or distributing its binaries.

## Lifecycle

The lifecycle is:

```text
STOPPED → STARTING → CONNECTING → CONNECTED → HEALTHY
                 ↘ FAILED / RECONNECTING / DEGRADED
HEALTHY → ROTATING → STARTING
```

- The manager starts at most one ConsoleClient process.
- The runtime config forces the Core's local SOCKS5 listener to an ephemeral
  **loopback** port and disables its local HTTP listener.
- It requires a `ListeningSocksProxyPort` notice and at least one `Tunnels`
  notice before probing actual HTTPS traffic through the SOCKS endpoint.
- A session expires at 30 minutes or earlier; the implementation caps
  `PSIPHON_SESSION_MAX_AGE_SECONDS` at 1800.
- Rotation fully terminates the old process, runs the existing bounded
  all-country proxy scan, selects an optional compatible managed upstream,
  starts a replacement, and publishes `HEALTHY` only after the new tunnel's
  HTTPS probe succeeds.
- Unexpected exits and failed probes use bounded exponential backoff. After
  `PSIPHON_MAX_RECONNECT_ATTEMPTS`, status is `FAILED`; the WSS service
  continues normally.
- During rotation, the manager stops issuing new VLESS backend leases before
  stopping the old Core process. A replacement is published only after its
  loopback SOCKS HTTPS health probe succeeds. Existing `/ws/{uuid}` sessions
  are not involved in this lifecycle.

There is no unbounded process overlap, no shell command execution, and no
unbounded output reader. Runtime configs are created with mode `0600` in
`PSIPHON_RUNTIME_DIR` (a local temporary directory by default), stale
manager-owned runtime files are removed before a new start, and the active
file is removed when its Core process stops. The existing operator config is
never modified.

## Managed upstream proxies

`PSIPHON_USE_MANAGED_UPSTREAM_PROXY=false` leaves the Core config's explicit
upstream setting, if any, under operator control. When it is `true`, Lumen:

1. runs the existing exact proxy-probe mechanism across every managed record
   before initial startup and each rotation, while keeping Psiphon results
   separate from the VLESS country-preference state;
2. considers only latest healthy managed records;
3. filters to the schemes documented by Core's `upstreamproxy` package:
   `http`, `socks4a`, and `socks5`;
4. picks the highest existing deterministic performance score, with stable
   proxy ID as the tie-breaker; and
5. writes the URL only to the 0600 ephemeral Core runtime config.

This selection is used **only** as the Core process's optional upstream. It
cannot change current VLESS proxy records, preferred country mappings, or an
active VLESS connection. An absent or incompatible managed upstream fails
Psiphon startup closed; it is never silently renamed to another scheme.

Existing catalog proxy records are not Psiphon server entries. A valid Core
server entry remains a separate operator-provided object.

## Egress and status

`PSIPHON_EGRESS_REGION` accepts one ISO 3166-1 alpha-2 value. When omitted,
the Core's documented connection-worker/server-racing behavior selects the
best-performing usable Core server. Lumen does not implement a competing
server-entry racer.

The authenticated admin endpoint is:

```text
GET /api/psiphon/status
```

It returns only safe status: lifecycle state, Core-reported region, real
setup latency, session age/remaining lifetime, rotation details, TCP health,
UDP state, and a tunnel-derived exit IP/location where available. It never
returns a local SOCKS port, PID, Core config, server entry, upstream endpoint,
credential, or raw Core notice.

## Standard VLESS/WS subscription profile

The Psiphon data plane has no custom client URI. When—and only when—the
official Core has a healthy tunnel, Lumen adds one ordinary VLESS-over-
WebSocket URI alongside the existing entry for each `vless-ws` account:

```text
Existing: vless://{same-uuid}@{domain}:443?...&type=ws&path=/ws/{uuid}
Psiphon:  vless://{same-uuid}@{domain}:443?...&type=ws&path=/ws/p-core/cq/{uuid}
```

The client uses normal VLESS/WS framing and the normal Lumen URI encoder.
The route path is the sole backend selector. It is not selected from headers,
query parameters, user agent, country, or a second UUID. The Psiphon profile
contains no Core server entry, Core config, local SOCKS port, internal
address, upstream proxy URL, or credential.

If the Core is disabled, not configured, unhealthy, rotating, or unavailable,
the Psiphon URI is omitted. If a client still uses the Psiphon path while the
Core is unavailable, the session closes without direct or normal-proxy
fallback. The primary `/ws/{uuid}` URI remains unaffected.

## TCP and UDP

The manager validates TCP by fetching a known HTTPS endpoint through the
Core's loopback SOCKS5 listener, then attempts an exit-IP lookup through that
same tunnel. Location lookup is optional and does not manufacture a healthy
or unhealthy result.

This integration does not configure an official packet-tunnel/TUN file
descriptor. It does not publish a UDP listener. Consequently:

```text
TCP: available only after a real Core tunnel is healthy
UDP: NOT_SUPPORTED
```

Railway's public networking exposes HTTP/HTTPS, and its public non-HTTP
feature is TCP Proxy; private networking allowing UDP does not create public
UDP ingress. Do not report UDP as working without a separate, end-to-end
validated Psiphon client/TUN deployment.

## Staging checklist

1. Keep `PSIPHON_ENABLED=false` in production while staging.
2. Mount a verified official binary and authorized config; do not commit
   either to the repository.
3. Confirm the Core emits loopback SOCKS/Tunnels notices.
4. Confirm the manager reaches `HEALTHY` only after an HTTPS request traverses
   that SOCKS endpoint.
5. Confirm the reported exit IP is tunnel-derived, not Railway egress.
6. Force one short-lifetime staging rotation and verify the old PID exits
   before the replacement becomes active.
7. Verify all eligible managed proxies are re-tested when managed upstream
   mode is enabled.
8. Verify `/ws/{uuid}`, legacy subscriptions, Multi-Location, and Raw TCP
   retain their existing behavior.
9. Verify the additional standard VLESS/WS entry appears only after Core
   health succeeds, has the identical UUID, and its path is exactly
   `/ws/p-core/cq/{uuid}`.
10. Verify a Core failure closes the Psiphon route without calling the normal
    outbound proxy resolver or interrupting `/ws/{uuid}`.

Without a verified official binary, authorized Core configuration/server
entries, and a live successful HTTPS-through-tunnel test, Psiphon remains
**unavailable** and its additional VLESS/WS profile is not advertised to
users.

## Railway build

The Dockerfile uses a `golang:1.26-bookworm` build stage and executes:

```text
go build -mod=vendor -trimpath -ldflags="-s -w" -o /out/psiphon-tunnel-core ./ConsoleClient
```

The final Python image copies only that official executable while retaining the
complete corresponding supplied source under `third_party/psiphon-tunnel-core`.
Port-forward mode requires no TUN device or Linux network capability. Packet/TUN
mode is intentionally rejected and UDP remains `NOT_SUPPORTED`. Railway must be
able to pull the Go builder image and provide ordinary outbound TCP/HTTPS.
