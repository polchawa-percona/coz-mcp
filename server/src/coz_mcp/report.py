"""Parser for sysbench 1.0 interval report lines (also emitted by the demo)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_LINE = re.compile(
    r"^\[\s*(?P<t>\d+)s\s*\]\s+thds:\s*(?P<thds>\d+)\s+(?P<kind>tps|eps):\s*(?P<rate>[0-9.]+)"
)


@dataclass(frozen=True)
class ReportLine:
    t_s: int  # end of the interval, seconds since the benchmark started
    threads: int
    rate: float  # transactions (tps) or events (eps) per second
    kind: str  # "tps" or "eps"


def parse_report_line(line: str) -> ReportLine | None:
    m = _LINE.match(line.strip())
    if not m:
        return None
    return ReportLine(
        t_s=int(m["t"]),
        threads=int(m["thds"]),
        rate=float(m["rate"]),
        kind=m["kind"],
    )
