import pytest

from coz_mcp.stats import compare


def test_compare_reports_delta_and_ci():
    r = compare(baseline=[100.0, 102.0, 98.0], target=[80.0, 82.0, 78.0])
    assert r["baseline_mean"] == pytest.approx(100.0)
    assert r["target_mean"] == pytest.approx(80.0)
    assert r["delta_pct"] == pytest.approx(-20.0)
    lo, hi = r["delta_pct_ci95"]
    assert lo < -20.0 < hi
    # Welch t with df=4: t=2.776, se of diff = sqrt(4/3 + 4/3) = 1.633
    assert hi - lo == pytest.approx(2 * 2.776 * 1.633, rel=1e-2)
    assert r["significant"] is True


def test_compare_not_significant_when_ci_crosses_zero():
    r = compare(baseline=[100.0, 110.0, 90.0], target=[101.0, 95.0, 108.0])
    assert r["significant"] is False


def test_compare_single_run_has_no_ci():
    r = compare(baseline=[100.0], target=[90.0])
    assert r["delta_pct"] == pytest.approx(-10.0)
    assert r["delta_pct_ci95"] is None
    assert r["significant"] is None
