import asyncio
import sys
import textwrap

import pytest

from coz_mcp.workload import Workload, WorkloadConfig, WorkloadError

FAKE = textwrap.dedent(
    """
    import sys, time
    n = int(sys.argv[1])
    print("Threads started!", flush=True)
    for i in range(1, n + 1):
        time.sleep(0.05)
        print(f"[ {i}s ] thds: 4 tps: {100 * i}.00 lat (ms,95%): 0.00", flush=True)
    print("args:" + " ".join(sys.argv[2:]), flush=True)
    """
)


@pytest.fixture
def fake_config(tmp_path):
    script = tmp_path / "fake_bench.py"
    script.write_text(FAKE)
    return WorkloadConfig(
        name="fake", binary=sys.executable, base_args=[str(script)], report_args=[]
    )


async def wait_finished(w: Workload, timeout=5.0):
    async with asyncio.timeout(timeout):
        while w.running:
            await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_collects_samples_and_delay(fake_config):
    delays = iter(range(0, 10_000, 1000))

    async def sample_delay():
        return next(delays)

    w = Workload(fake_config, delay_sampler=sample_delay)
    await w.start(["3"])
    await wait_finished(w)
    s = w.samples()
    assert [x.rate for x in s] == [100.0, 200.0, 300.0]
    assert [x.global_delay_ns for x in s] == [0, 1000, 2000]
    assert all(x.interval_s == 1.0 for x in s)
    assert s[0].t_end < s[1].t_end < s[2].t_end
    assert w.exit_code == 0
    assert any(line.startswith("args:") for line in w.tail(5))


@pytest.mark.anyio
async def test_delay_sampler_failure_records_none(fake_config):
    async def broken():
        raise ConnectionError("no coz")

    w = Workload(fake_config, delay_sampler=broken)
    await w.start(["2"])
    await wait_finished(w)
    assert [x.global_delay_ns for x in w.samples()] == [None, None]


@pytest.mark.anyio
async def test_rejects_rate_option(fake_config):
    w = Workload(fake_config)
    with pytest.raises(WorkloadError, match="--rate"):
        await w.start(["--rate=100"])
    with pytest.raises(WorkloadError, match="--rate"):
        await w.start(["--rate", "100"])


@pytest.mark.anyio
async def test_only_one_workload_at_a_time(fake_config):
    w = Workload(fake_config)
    await w.start(["50"])
    try:
        with pytest.raises(WorkloadError, match="already running"):
            await w.start(["1"])
    finally:
        await w.stop()
    assert not w.running


@pytest.mark.anyio
async def test_stop_kills_process(fake_config):
    w = Workload(fake_config)
    await w.start(["1000"])
    await asyncio.sleep(0.1)
    await w.stop()
    assert not w.running
    assert w.exit_code is not None


@pytest.mark.anyio
async def test_last_seconds_filter(fake_config):
    w = Workload(fake_config)
    await w.start(["4"])
    await wait_finished(w)
    last = w.samples(last_s=0.12)
    assert 1 <= len(last) <= 3
    assert last[-1].rate == 400.0


@pytest.mark.anyio
async def test_env_is_passed(tmp_path):
    script = tmp_path / "env.py"
    script.write_text("import os; print('X=' + os.environ['COZ_MCP_TEST_X'])")
    cfg = WorkloadConfig(
        name="env", binary=sys.executable, base_args=[str(script)],
        env={"COZ_MCP_TEST_X": "42"},
    )
    w = Workload(cfg)
    await w.start([])
    await wait_finished(w)
    assert "X=42" in w.tail()


def test_parse_cpus():
    from coz_mcp.workload import parse_cpus
    assert parse_cpus("8-10,20") == {8, 9, 10, 20}
    assert parse_cpus("3") == {3}


@pytest.mark.anyio
async def test_cpus_pin_child(tmp_path):
    script = tmp_path / "aff.py"
    script.write_text("import os; print('cpus=' + ','.join(map(str, sorted(os.sched_getaffinity(0)))))")
    cfg = WorkloadConfig(name="aff", binary=sys.executable, base_args=[str(script)], cpus="0-1")
    w = Workload(cfg)
    await w.start([])
    await wait_finished(w)
    assert "cpus=0,1" in w.tail()
