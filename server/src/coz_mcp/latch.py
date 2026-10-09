"""Ranking of latch acquire sites from two libcoz latch_stats snapshots."""

from __future__ import annotations

from typing import Any, Literal

Metric = Literal["wait", "hold"]


def percentile_ns(hist: list[int], q: float) -> int | None:
    """Upper bound of the log2 bucket holding the q-quantile (bucket i < 2^i ns)."""
    total = sum(hist)
    if total == 0:
        return None
    need = q * total
    seen = 0
    for i, n in enumerate(hist):
        seen += n
        if seen >= need:
            return 1 << i
    return 1 << (len(hist) - 1)


def _key(s: dict[str, Any]) -> tuple[str, str, str]:
    return (s["site"], s["name"], s["mode"])


def rank_latches(before: dict[str, Any], after: dict[str, Any], metric: Metric = "hold",
                 limit: int = 20, latch: str | None = None,
                 path_filter: str | None = None) -> dict[str, Any]:
    window_s = max((after["now_ns"] - before["now_ns"]) / 1e9, 1e-9)
    old = {_key(s): s for s in before["latches"]}
    count_f = "count" if metric == "wait" else "hold_count"
    sum_f = f"{metric}_ns"
    hist_f = f"{metric}_hist"
    reset = False
    rows = []
    for s in after["latches"]:
        o = old.get(_key(s))
        count = s[count_f] - (o[count_f] if o else 0)
        total = s[sum_f] - (o[sum_f] if o else 0)
        if count < 0 or total < 0:
            reset = True  # counters were reset in between: use the new values
            count, total, o = s[count_f], s[sum_f], None
        if count <= 0:
            continue
        if latch and latch not in s["name"]:
            continue
        if path_filter and path_filter not in s["site"]:
            continue
        hist = [a - (o[hist_f][i] if o else 0) for i, a in enumerate(s[hist_f])]
        rows.append({
            "site": s["site"],
            "name": s["name"],
            "mode": s["mode"],
            "count_per_s": count / window_s,
            "total_ms_per_s": total / 1e6 / window_s,
            "avg_ns": total / count,
            "p50_ns": percentile_ns(hist, 0.5),
            "p99_ns": percentile_ns(hist, 0.99),
            "max_ns_since_reset": s[f"{metric}_max_ns"],
        })
    rows.sort(key=lambda r: -r["total_ms_per_s"])
    return {
        "metric": metric,
        "window_s": window_s,
        "latches": rows[:limit],
        "unmatched_releases": after.get("unmatched_releases", 0),
        "stack_overflows": after.get("stack_overflows", 0),
        "reset_detected": reset,
    }
