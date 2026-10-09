"""Baseline vs target comparison with a Welch t confidence interval."""

from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

# Two-sided 95% Student t critical values by degrees of freedom.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
        8: 2.306, 9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086,
        30: 2.042, 60: 2.000}


def _t95(df: float) -> float:
    for k in sorted(_T95):
        if df <= k:
            return _T95[k]
    return 1.96


def compare(baseline: Sequence[float], target: Sequence[float]) -> dict[str, Any]:
    b_mean = statistics.fmean(baseline)
    t_mean = statistics.fmean(target)
    out: dict[str, Any] = {
        "baseline_mean": b_mean,
        "target_mean": t_mean,
        "baseline_stdev": statistics.stdev(baseline) if len(baseline) > 1 else None,
        "target_stdev": statistics.stdev(target) if len(target) > 1 else None,
        "delta_pct": (t_mean - b_mean) / b_mean * 100.0,
        "delta_pct_ci95": None,
        "significant": None,
    }
    if len(baseline) < 2 or len(target) < 2:
        return out
    vb = statistics.variance(baseline) / len(baseline)
    vt = statistics.variance(target) / len(target)
    se = math.sqrt(vb + vt)
    if se == 0:
        df = len(baseline) + len(target) - 2
    else:
        df = (vb + vt) ** 2 / (vb**2 / (len(baseline) - 1) + vt**2 / (len(target) - 1))
    half = _t95(df) * se / b_mean * 100.0
    d = out["delta_pct"]
    out["delta_pct_ci95"] = [d - half, d + half]
    out["significant"] = not (d - half <= 0.0 <= d + half)
    return out
