import pytest

from coz_mcp.report import ReportLine, parse_report_line


def test_parses_sysbench_oltp_line():
    line = (
        "[ 12s ] thds: 64 tps: 1234.56 qps: 24691.20 "
        "(r/w/o: 17283.84/4938.24/2469.12) lat (ms,95%): 3.25 err/s: 0.00 "
        "reconn/s: 0.00"
    )
    assert parse_report_line(line) == ReportLine(
        t_s=12, threads=64, rate=1234.56, kind="tps"
    )


def test_parses_sysbench_events_line():
    line = "[ 1s ] thds: 2 eps: 8930.65 lat (ms,95%): 0.23"
    assert parse_report_line(line) == ReportLine(
        t_s=1, threads=2, rate=8930.65, kind="eps"
    )


def test_parses_demo_line():
    line = "[ 3s ] thds: 4 tps: 75909.00 lat (ms,95%): 0.00\n"
    assert parse_report_line(line) == ReportLine(
        t_s=3, threads=4, rate=75909.0, kind="tps"
    )


@pytest.mark.parametrize(
    "line",
    [
        "",
        "total transactions: 215663",
        "Threads started!",
        "[ 1s ] thds: 4",
        "SQL statistics:",
    ],
)
def test_ignores_other_lines(line):
    assert parse_report_line(line) is None
