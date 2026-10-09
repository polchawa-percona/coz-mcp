"""MCP server tests through the in-memory client, with fake Coz and workload."""

import asyncio
import json
import time

import pytest
from mcp import Client

from coz_mcp.control import CozError
from coz_mcp.metrics import Sample
from coz_mcp.server import build_server
from coz_mcp.workload import WorkloadError

pytestmark = pytest.mark.anyio


class FakeCoz:
    def __init__(self):
        self.calls = []
        self.active = None  # (kind, percent)
        self.delay = 0
        self.hot_calls = 0
        self.latch_enabled = False
        self.snapshots = 0

    async def request(self, cmd, **args):
        self.calls.append((cmd, args))
        if cmd == "status":
            return {"mode": "manual", "experiment_active": self.active is not None,
                    "global_delay_ns": self.delay, "slowdown_ns": 0, "threads": 4}
        if cmd == "hot_lines":
            self.hot_calls += 1
            k = self.hot_calls
            return {"total_samples": 100 * k, "lines": [
                {"line": "/src/a.cc:10", "samples": 50 * k},
                {"line": "/src/b.cc:20", "samples": 40 + 5 * k},
                {"line": "/usr/include/x.h:1", "samples": 10},
            ]}
        if cmd == "start":
            if args["line"] == "missing.cc:1":
                raise CozError("line not found: missing.cc:1")
            self.active = (args["kind"], args["percent"])
            return {"line": "/src/" + args["line"], "start_ns": 1, "samples": 5}
        if cmd == "stop":
            kind, pct = self.active
            self.active = None
            return {"kind": kind, "percent": pct, "global_delay_ns_delta": 0,
                    "slowdown_ns_delta": 1000, "selected_samples": 3,
                    "auto_stopped": False}
        if cmd == "latch_stats":
            self.latch_enabled = args["enable"]
            return {"enabled": args["enable"]}
        if cmd == "latch_stats_reset":
            return {"reset": True}
        if cmd == "latch_stats_snapshot":
            self.snapshots += 1
            k = self.snapshots
            hist = [0] * 40
            hist[11] = 10 * k
            return {"enabled": self.latch_enabled, "now_ns": k * 1_000_000_000,
                    "unmatched_releases": 0, "stack_overflows": 0, "latches": [
                        {"site": "/src/trx0sys.cc:100", "name": "trx_sys_mutex",
                         "mode": "mutex", "count": 10 * k, "wait_ns": 5000 * k,
                         "wait_max_ns": 900, "hold_count": 10 * k,
                         "hold_ns": 20000 * k, "hold_max_ns": 3000,
                         "wait_hist": hist, "hold_hist": hist}]}
        raise CozError(f"unknown cmd: {cmd}")

    async def global_delay_ns(self):
        return self.delay


class FakeWorkload:
    """Produces a sample every 20 ms; slowdown experiments cost percent*0.4 TPS."""

    class _Cfg:
        name = "fake"

    def __init__(self, coz):
        self.coz = coz
        self.config = self._Cfg()
        self._samples = []
        self._task = None
        self.args = []
        self.started_at = None
        self.exit_code = None

    @property
    def running(self):
        return self._task is not None and not self._task.done()

    async def start(self, args):
        if any(a.startswith("--rate") for a in args):
            raise WorkloadError("--rate is not allowed")
        if self.running:
            raise WorkloadError("workload already running; stop it first")
        self.args = list(args)
        self._task = asyncio.create_task(self._produce())
        return ["fake", *args]

    async def _produce(self):
        while True:
            await asyncio.sleep(0.02)
            rate = 100.0
            if self.coz.active and self.coz.active[0] == "slowdown":
                rate -= self.coz.active[1] * 0.4
            self._samples.append(Sample(t_end=time.monotonic(), interval_s=0.02,
                                        rate=rate, global_delay_ns=self.coz.delay))

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.exit_code = -15

    def kill_now(self):
        if self._task:
            self._task.cancel()

    def samples(self, last_s=None):
        s = list(self._samples)
        if last_s is None or not s:
            return s
        return [x for x in s if x.t_end >= s[-1].t_end - last_s]

    def tail(self, n=20):
        return ["[ 1s ] thds: 4 tps: 100.00"]


@pytest.fixture
def fakes():
    coz = FakeCoz()
    return coz, FakeWorkload(coz)


@pytest.fixture
async def client(fakes):
    coz, wl = fakes
    server = build_server(coz=coz, workload=wl, latch_poll_s=0)
    async with Client(server) as c:
        yield c
    await wl.stop()


def data(result):
    assert not result.is_error, result.content
    return result.structured_content


async def test_lists_tools_resources_prompts(client):
    tools = {t.name for t in (await client.list_tools()).tools}
    assert {"coz_status", "hot_lines", "start_workload", "stop_workload",
            "workload_tps", "run_experiment"} <= tools
    resources = {str(r.uri) for r in (await client.list_resources()).resources}
    assert {"coz://experiments", "coz://hot-lines"} <= resources
    prompts = {p.name for p in (await client.list_prompts()).prompts}
    assert "find_bottleneck" in prompts


async def test_status_and_workload_lifecycle(client, fakes):
    st = data(await client.call_tool("coz_status", {}))
    assert st["coz"]["mode"] == "manual"
    assert st["workload"]["running"] is False

    started = data(await client.call_tool("start_workload", {"args": ["--threads=8"]}))
    assert started["command"] == ["fake", "--threads=8"]
    await asyncio.sleep(0.15)
    tps = data(await client.call_tool("workload_tps", {"last_s": 1}))
    assert tps["mean_tps"] == pytest.approx(100.0)
    assert len(tps["series"]) >= 3

    data(await client.call_tool("stop_workload", {}))
    st = data(await client.call_tool("coz_status", {}))
    assert st["workload"]["running"] is False


async def test_rate_is_rejected(client):
    r = await client.call_tool("start_workload", {"args": ["--rate=100"]})
    assert r.is_error
    assert "--rate" in r.content[0].text


async def test_hot_lines_ranks_samples_in_window(client):
    r = data(await client.call_tool("hot_lines", {"window_s": 0.05, "limit": 5}))
    # Second snapshot minus first: a.cc +50, b.cc +5, x.h +0 (dropped).
    assert [x["line"] for x in r["lines"]] == ["/src/a.cc:10", "/src/b.cc:20"]
    assert r["lines"][0]["samples"] == 50
    assert r["lines"][0]["share_pct"] == pytest.approx(50.0)
    filtered = data(await client.call_tool(
        "hot_lines", {"window_s": 0.05, "path_filter": "b.cc"}))
    assert [x["line"] for x in filtered["lines"]] == ["/src/b.cc:20"]
    text = (await client.read_resource("coz://hot-lines")).contents[0].text
    assert "/src/b.cc:20" in text


async def test_run_experiment_alternates_baseline_and_target(client, fakes):
    coz, _ = fakes
    data(await client.call_tool("start_workload", {"args": []}))
    progress = []

    async def on_progress(p, total, msg):
        progress.append((p, total, msg))

    r = await client.call_tool(
        "run_experiment",
        {"line": "a.cc:10", "kind": "slowdown", "percent": 50,
         "duration_s": 0.3, "repeats": 2, "warmup_s": 0.05, "cooldown_s": 0.0},
        progress_callback=on_progress,
    )
    res = data(r)
    starts = [a["percent"] for c, a in coz.calls if c == "start"]
    assert starts == [0, 50, 0, 50]
    assert res["summary"]["baseline_mean"] == pytest.approx(100.0)
    assert res["summary"]["target_mean"] == pytest.approx(80.0)
    assert res["summary"]["delta_pct"] == pytest.approx(-20.0)
    assert len(res["runs"]) == 4
    assert progress and progress[-1][0] == progress[-1][1]

    log = json.loads((await client.read_resource("coz://experiments")).contents[0].text)
    assert log[-1]["line"] == "a.cc:10"
    assert log[-1]["summary"]["delta_pct"] == pytest.approx(-20.0)


async def test_run_experiment_needs_running_workload(client):
    r = await client.call_tool(
        "run_experiment", {"line": "a.cc:10", "kind": "speedup", "percent": 20})
    assert r.is_error
    assert "start_workload" in r.content[0].text


async def test_run_experiment_reports_coz_errors_and_stops(client, fakes):
    coz, _ = fakes
    data(await client.call_tool("start_workload", {"args": []}))
    r = await client.call_tool(
        "run_experiment", {"line": "missing.cc:1", "kind": "speedup", "percent": 20,
                           "duration_s": 0.1, "repeats": 1, "warmup_s": 0})
    assert r.is_error
    assert "line not found" in r.content[0].text
    assert coz.active is None


async def test_only_one_experiment_at_a_time(client):
    data(await client.call_tool("start_workload", {"args": []}))
    args = {"line": "a.cc:10", "kind": "slowdown", "percent": 10,
            "duration_s": 0.3, "repeats": 1, "warmup_s": 0, "cooldown_s": 0}
    first = asyncio.create_task(client.call_tool("run_experiment", args))
    await asyncio.sleep(0.05)
    second = await client.call_tool("run_experiment", args)
    assert second.is_error
    assert "already running" in second.content[0].text
    assert not (await first).is_error


async def test_find_bottleneck_prompt(client):
    p = await client.get_prompt("find_bottleneck", {"candidates": "3"})
    text = p.messages[0].content.text
    assert "hot_lines" in text and "run_experiment" in text and "3" in text


async def test_latch_tools(client, fakes):
    coz, _ = fakes
    r = data(await client.call_tool("latch_stats_enable", {"on": True}))
    assert r["enabled"] is True and coz.latch_enabled
    top = data(await client.call_tool("latch_top", {"metric": "hold", "window_s": 0.01}))
    row = top["latches"][0]
    assert row["site"] == "/src/trx0sys.cc:100"
    assert row["count_per_s"] == pytest.approx(10.0)
    assert row["total_ms_per_s"] == pytest.approx(0.02)
    assert "caveats" in top
    text = (await client.read_resource("coz://latches/top")).contents[0].text
    assert "trx_sys_mutex" in text
    data(await client.call_tool("latch_stats_reset", {}))


async def test_latch_top_requires_enabled_stats(client):
    r = await client.call_tool("latch_top", {"window_s": 0.01})
    assert r.is_error
    assert "latch_stats_enable" in r.content[0].text


async def test_latch_poller_publishes_updates(fakes):
    coz, wl = fakes
    server = build_server(coz=coz, workload=wl, latch_poll_s=0.05)
    async with Client(server) as c:
        await c.call_tool("latch_stats_enable", {"on": True})
        async with c.listen(resource_subscriptions=["coz://latches/top"]) as sub:
            async with asyncio.timeout(5):
                async for event in sub:
                    assert event.uri == "coz://latches/top"
                    break
        text = (await c.read_resource("coz://latches/top")).contents[0].text
        assert "trx_sys_mutex" in json.loads(text)["hold"]["latches"][0]["name"]


async def test_cancelled_experiment_stops_coz(client, fakes):
    coz, _ = fakes
    data(await client.call_tool("start_workload", {"args": []}))
    t = asyncio.create_task(client.call_tool("run_experiment", {
        "line": "a.cc:10", "kind": "slowdown", "percent": 50,
        "duration_s": 5, "repeats": 1, "warmup_s": 0}))
    async with asyncio.timeout(2):
        while coz.active is None:
            await asyncio.sleep(0.01)
    t.cancel()
    with pytest.raises(BaseException):
        await t
    async with asyncio.timeout(2):
        while coz.active is not None:
            await asyncio.sleep(0.01)
