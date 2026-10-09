"""Throughput over an experiment window, with Coz virtual-delay correction.

Coz implements a virtual speedup by pausing every other thread; the pauses
stretch wall time. For each report interval we subtract the virtual delay
inserted during that interval (difference of Coz's global delay counter sampled
at the end of this and the previous interval) and divide the interval's
transactions by the remaining time.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

_NS = 1e9
# Same guard as upstream Coz: never shrink an interval below 10%.
MIN_EFFECTIVE_FRACTION = 0.1


@dataclass(frozen=True)
class Sample:
    t_end: float  # monotonic time (s) when the interval ended
    interval_s: float
    rate: float  # transactions per second during the interval
    global_delay_ns: int | None  # Coz global delay sampled at t_end


@dataclass(frozen=True)
class WindowResult:
    intervals: int
    raw_tps: float | None
    corrected_tps: float | None
    clamped_intervals: int = 0
    uncorrected_intervals: int = 0


def window_tps(
    samples: Sequence[Sample], start: float, end: float, corrected: bool
) -> WindowResult:
    eps = 1e-9
    txns = 0.0
    wall = 0.0
    effective = 0.0
    used = clamped = uncorrected = 0
    prev: Sample | None = None
    for s in samples:
        begin = s.t_end - s.interval_s
        if begin >= start - eps and s.t_end <= end + eps:
            n = s.rate * s.interval_s
            txns += n
            wall += s.interval_s
            used += 1
            length = s.interval_s
            if corrected:
                if (
                    prev is None
                    or prev.global_delay_ns is None
                    or s.global_delay_ns is None
                ):
                    uncorrected += 1
                else:
                    delay = (s.global_delay_ns - prev.global_delay_ns) / _NS
                    length = s.interval_s - max(delay, 0.0)
                    floor = MIN_EFFECTIVE_FRACTION * s.interval_s
                    if length < floor:
                        length = floor
                        clamped += 1
            effective += length
        prev = s
    if used == 0:
        return WindowResult(intervals=0, raw_tps=None, corrected_tps=None)
    return WindowResult(
        intervals=used,
        raw_tps=txns / wall,
        corrected_tps=txns / effective,
        clamped_intervals=clamped,
        uncorrected_intervals=uncorrected,
    )
