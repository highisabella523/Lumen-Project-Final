"""Pure, deterministic performance ranking for managed proxy records.

This module has no network or persistence side effects.  It deliberately ranks
only results produced by the exact-proxy probe: a result for one proxy can
never make another endpoint eligible.
"""
from __future__ import annotations

from typing import Iterable

REQUIRED_TARGETS = frozenset(("https://cloudflare.com", "https://google.com"))
MAX_METRIC_MS = 120_000


def bounded_ms(value: object) -> float | None:
    """Accept only finite non-negative latency values from a probe result."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0 or number > MAX_METRIC_MS or number != number:
        return None
    return round(number, 3)


def average_check_ms(result: dict, field: str) -> float:
    values = [
        value for value in (
            bounded_ms(check.get(field))
            for check in (result.get("checks") or [])
            if isinstance(check, dict) and check.get("ok")
        )
        if value is not None
    ]
    return round(sum(values) / len(values), 3) if values else float(MAX_METRIC_MS)


def current_healthy(result: object) -> bool:
    if not isinstance(result, dict) or result.get("overall_status") != "healthy":
        return False
    checks = result.get("checks") or []
    return (
        isinstance(checks, list)
        and {item.get("target") for item in checks if isinstance(item, dict)} == REQUIRED_TARGETS
        and all(bool(item.get("ok")) for item in checks if isinstance(item, dict))
    )


def performance_score(result: dict) -> int:
    """Higher is better; reliability outweighs small latency differences.

    The score is intentionally transparent:

    ``reliability * 1,000,000 - total_ms * 20 - connect_ms * 5
    - request_ms * 2``.

    Reliability is historical success/sample count for this stable proxy ID.
    Only a currently healthy result is eligible, so the score cannot revive a
    proxy whose most recent complete two-target probe failed.  The latency
    terms are averages of real successful Cloudflare and Google probes.
    """
    samples = max(1, min(int(result.get("sample_count") or 1), 10_000))
    successes = max(0, min(int(result.get("success_count") or 0), samples))
    reliability = successes / samples
    total_ms = min(average_check_ms(result, "total_ms"), 30_000.0)
    connect_ms = min(average_check_ms(result, "connect_ms"), 30_000.0)
    request_ms = min(average_check_ms(result, "request_ms"), 30_000.0)
    return int(round(
        reliability * 1_000_000
        - total_ms * 20
        - connect_ms * 5
        - request_ms * 2
    ))


def preferred_proxy_id(record_ids: Iterable[str], results: dict[str, dict]) -> str | None:
    """Return one stable ID, or None when no current exact result is healthy."""
    candidates: list[tuple[int, float, float, float, str]] = []
    for raw_id in record_ids:
        proxy_id = str(raw_id or "")
        result = results.get(proxy_id)
        if not current_healthy(result):
            continue
        candidates.append((
            -performance_score(result),
            average_check_ms(result, "total_ms"),
            average_check_ms(result, "connect_ms"),
            average_check_ms(result, "request_ms"),
            proxy_id,
        ))
    if not candidates:
        return None
    # The stable proxy ID is the last tie-breaker. This removes source-order
    # randomness while preserving proxy identity and avoiding load balancing.
    return min(candidates)[-1]