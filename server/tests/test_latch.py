import pytest

from coz_mcp.latch import percentile_ns, rank_latches

B = 40  # histogram buckets


def hist(**buckets):
    h = [0] * B
    for k, v in buckets.items():
        h[int(k[1:])] = v
    return h


def site(name, mode, count, wait_ns, hold_ns, wait_hist=None, hold_hist=None,
         site_="/src/a.cc:10", wait_max=0, hold_max=0):
    return {"site": site_, "name": name, "mode": mode, "count": count,
            "wait_ns": wait_ns, "wait_max_ns": wait_max, "hold_count": count,
            "hold_ns": hold_ns, "hold_max_ns": hold_max,
            "wait_hist": wait_hist or [0] * B, "hold_hist": hold_hist or [0] * B}


def snap(now_ns, *sites):
    return {"enabled": True, "now_ns": now_ns, "unmatched_releases": 0,
            "stack_overflows": 0, "latches": list(sites)}


def test_percentile_uses_bucket_upper_bound():
    h = hist(b3=90, b10=10)  # bucket 3 = [4, 8) ns, bucket 10 = [512, 1024) ns
    assert percentile_ns(h, 0.5) == 8
    assert percentile_ns(h, 0.95) == 1024
    assert percentile_ns([0] * B, 0.5) is None


def test_rank_by_hold_time_in_window():
    before = snap(0,
                  site("trx_sys", "mutex", 100, 1_000, 50_000, site_="/src/t.cc:1"),
                  site("lock_sys", "x", 10, 0, 10_000, site_="/src/l.cc:2"))
    after = snap(2_000_000_000,
                 site("trx_sys", "mutex", 300, 3_000, 150_000, site_="/src/t.cc:1",
                      hold_hist=hist(b9=200), hold_max=900),
                 site("lock_sys", "x", 1010, 0, 2_010_000, site_="/src/l.cc:2",
                      hold_hist=hist(b11=1000)),
                 site("new", "s", 5, 50, 500, site_="/src/n.cc:3"))
    r = rank_latches(before, after, metric="hold", limit=10)
    assert [x["site"] for x in r["latches"]] == ["/src/l.cc:2", "/src/t.cc:1", "/src/n.cc:3"]
    top = r["latches"][0]
    assert r["window_s"] == pytest.approx(2.0)
    assert top["count_per_s"] == pytest.approx(500.0)
    assert top["total_ms_per_s"] == pytest.approx(1.0)  # 2 ms over 2 s
    assert top["avg_ns"] == pytest.approx(2000.0)
    assert top["p50_ns"] == 2048
    second = r["latches"][1]
    assert second["max_ns_since_reset"] == 900


def test_rank_by_wait_and_filters():
    before = snap(0)
    after = snap(1_000_000_000,
                 site("a", "x", 10, 9_000, 0, site_="/src/a.cc:1"),
                 site("b", "s", 10, 1_000, 0, site_="/usr/include/b.h:2"))
    r = rank_latches(before, after, metric="wait", limit=1)
    assert [x["name"] for x in r["latches"]] == ["a"]
    r = rank_latches(before, after, metric="wait", path_filter="b.h")
    assert [x["name"] for x in r["latches"]] == ["b"]
    r = rank_latches(before, after, metric="wait", latch="b")
    assert [x["name"] for x in r["latches"]] == ["b"]


def test_counters_reset_between_snapshots_is_reported():
    before = snap(0, site("a", "x", 100, 9_000, 9_000))
    after = snap(1_000_000_000, site("a", "x", 10, 100, 100))
    r = rank_latches(before, after, metric="hold")
    assert r["reset_detected"] is True


def test_handoff_and_lock_depth_fields_passed_through():
    before = dict(snap(0), handoff_acquires=100)
    after = dict(snap(2_000_000_000, site("block", "x", 10, 100, 0)),
                 handoff_acquires=500, held_now=3, lock_depth_raised=1,
                 lock_depth_max=2)
    r = rank_latches(before, after, metric="wait")
    assert r["handoff_acquires_per_s"] == pytest.approx(200.0)
    assert (r["held_now"], r["lock_depth_raised"], r["lock_depth_max"]) == (3, 1, 2)


def test_old_libcoz_snapshot_has_no_handoff_fields():
    r = rank_latches(snap(0), snap(1_000_000_000), metric="hold")
    assert "handoff_acquires_per_s" not in r and "lock_depth_raised" not in r
