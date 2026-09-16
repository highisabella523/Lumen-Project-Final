# Managed proxy performance routing

## Scope

This feature applies only to records in the private managed proxy repository.
It does not change the Railway HTTP/WSS listener, VLESS framing, subscription
identity, authentication, or direct/custom routes.

Each managed test uses the **one exact configured proxy endpoint** for:

1. `GET https://cloudflare.com/`
2. `GET https://google.com/`
3. public exit-IP lookup at `api.ipify.org`
4. optional exit-location lookup at `ipapi.co`

DNS for all of those destinations is presented to the selected HTTP CONNECT or
SOCKS5 proxy; the Lumen process does not substitute a direct target connection.
An IP/location lookup failure is diagnostic-only. A proxy remains healthy when
the two required HTTPS checks succeed.

## Stored data and privacy

Only safe diagnostics are saved in `code_state.json`:

- stable proxy ID and repository country code;
- test timestamp and current status;
- measured connect, proxy-handshake, HTTPS request, and total milliseconds;
- observed exit IP, optional exit location, score, and success history;
- country → preferred stable proxy-ID mapping.

Endpoint URLs, proxy protocol, usernames, passwords, payloads, and request
headers are never copied into health metadata or browser-facing catalog data.

## Ranking

A country is ranked only after **every current record in that country** has
completed a bounded scan. The latest failed result is ineligible. Among
currently healthy records, higher is better:

```text
score =
  reliability × 1,000,000
  − mean_total_ms × 20
  − mean_connect_ms × 5
  − mean_request_ms × 2
```

`reliability` is `successful_complete_scans / scans` for the stable proxy ID.
The final deterministic tie-breakers are lower total latency, lower connection
latency, lower request latency, then lexicographically lower stable ID.

This prevents repository order, historical source percentages, and random
selection from affecting a route. Throughput is deliberately not guessed or
included until a bounded transfer measurement is added.

## Routing contract

New Multi-Location groups can use `country_preferred`. They contain exactly two
different countries and generate one client choice per country. For each new
connection, Lumen resolves that selected country to the one current preferred
healthy stable ID and hands exactly one endpoint to the existing outbound
connector. The chosen socket remains fixed for the session.

There is no health sorting in the data plane, no endpoint list, no retry to a
different proxy, no country switch, and no direct fallback. If a country has no
current healthy preferred record, the new connection fails closed.

Existing `selection_mode: explicit` Multi-Location records preserve their
stored `location → proxy_id` mapping unchanged. Raw TCP Multi-Location remains
explicit because its verified SNI profiles map to fixed proxies.

## Test and refresh behavior

- A repository catalog change queues a complete scan only for affected
  countries.
- Admins can start one `Test all proxies` job or a single exact-proxy test.
- Concurrent all-country requests coalesce into one worker.
- The worker uses a semaphore (default four records; configurable from one to
  eight) and retains prior successful results while a refresh runs.
- A six-hour scheduled scan is enabled by default and can be disabled with
  `LUMEN_PROXY_PERFORMANCE_REFRESH_SECONDS=0`.

The dashboard shows all managed proxy records to authenticated admins. Public
subscription output exposes country choices and measured country latency only;
it does not expose proxy IDs, endpoints, protocol types, or credentials.