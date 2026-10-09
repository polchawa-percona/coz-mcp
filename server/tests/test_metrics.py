import pytest

from coz_mcp.metrics import Sample, window_tps

S = 1_000_000_000  # ns per second


def samples(rates, delays=None, start=0.0):
    """One sample per second ending at start+1, start+2, ...; delays in ns."""
    delays = delays or [None] * len(rates)
    return [
        Sample(t_end=start + i + 1, interval_s=1.0, rate=r, global_delay_ns=d)
        for i, (r, d) in enumerate(zip(rates, delays))
    ]


def test_raw_tps_uses_only_intervals_fully_inside_window():
    s = samples([100, 200, 300, 400, 500])
    # Window [1.5, 4.0]: only intervals (2,3] and (3,4] are fully inside.
    r = window_tps(s, start=1.5, end=4.0, corrected=False)
    assert r.intervals == 2
    assert r.raw_tps == pytest.approx(350.0)
    assert r.corrected_tps == pytest.approx(350.0)


def test_correction_subtracts_delay_inserted_in_each_interval():
    # Delay counter sampled at the end of each interval.
    s = samples([100, 100, 100, 100], delays=[7, 7, 7 + S // 2, 7 + S // 2 + S // 4])
    r = window_tps(s, start=1.0, end=4.0, corrected=True)
    # Intervals: 100/(1-0), 100/(1-0.5), 100/(1-0.25) -> 300 txns over 2.25 s.
    assert r.raw_tps == pytest.approx(100.0)
    assert r.corrected_tps == pytest.approx(300 / 2.25)


def test_interval_without_previous_delay_sample_is_not_corrected():
    s = samples([100, 100], delays=[S, S + S // 2])
    r = window_tps(s, start=0.0, end=2.0, corrected=True)
    assert r.corrected_tps == pytest.approx(200 / 1.5)
    assert r.uncorrected_intervals == 1


def test_correction_uses_previous_sample_outside_window_as_base():
    s = samples([100, 100, 100], delays=[S, S + S // 2, S + S // 2])
    r = window_tps(s, start=1.0, end=3.0, corrected=True)
    # Intervals 2 and 3: delays 0.5 s and 0 s -> 200 txns over 1.5 s.
    assert r.corrected_tps == pytest.approx(200 / 1.5)


def test_correction_is_clamped_to_ten_percent_of_interval():
    s = samples([100, 100], delays=[0, 5 * S])
    r = window_tps(s, start=1.0, end=2.0, corrected=True)
    assert r.corrected_tps == pytest.approx(100 / 0.1)
    assert r.clamped_intervals == 1


def test_missing_delay_sample_disables_correction_for_that_interval():
    s = samples([100, 100, 100], delays=[0, S // 2, None])
    r = window_tps(s, start=1.0, end=3.0, corrected=True)
    assert r.corrected_tps == pytest.approx(200 / 1.5)
    assert r.uncorrected_intervals == 1


def test_empty_window():
    r = window_tps(samples([100]), start=5.0, end=6.0, corrected=True)
    assert r.intervals == 0
    assert r.raw_tps is None
    assert r.corrected_tps is None
