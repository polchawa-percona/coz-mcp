"""Integration tests: the real demo under the coz-mcp libcoz.

Need a built tree (COZ_MCP_BUILD_DIR, default ~/builds/coz-mcp-release) and
kernel.perf_event_paranoid <= 1; skipped otherwise.
"""

import asyncio
import json
import os
import re
from pathlib import Path

import pytest

from coz_mcp.config import demo_workload
from coz_mcp.control import CozControl, CozError
from coz_mcp.metrics import window_tps
from coz_mcp.workload import Workload

REPO = Path(__file__).resolve().parents[3]
BUILD = Path(os.environ.get("COZ_MCP_BUILD_DIR", Path.home() / "builds/coz-mcp-release"))


def _paranoid() -> int:
    try:
        return int(Path("/proc/sys/kernel/perf_event_paranoid").read_text())
    except OSError:
        return 99


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not (BUILD / "demo" / "demo").exists()
        or not (BUILD / "coz" / "libcoz" / "libcoz.so").exists(),
        reason=f"no coz-mcp build in {BUILD}",
    ),
    pytest.mark.skipif(_paranoid() > 1, reason="perf_event_paranoid > 1"),
]


def demo_line(tag: str) -> str:
    """file:line of the demo line marked '// COZ-LINE: <tag>'."""
    src = REPO / "demo" / "demo.cc"
    for n, text in enumerate(src.read_text().splitlines(), 1):
        if re.search(rf"//\s*COZ-LINE:\s*{tag}\b", text):
            return f"demo.cc:{n}"
    raise AssertionError(f"no COZ-LINE {tag}")


class Demo:
    def __init__(self, tmp_path, args):
        self.sock = str(tmp_path / "coz.sock")
        self.profile = str(tmp_path / "profile.coz")
        self.workload = Workload(
            demo_workload(BUILD, self.sock, self.profile),
        )
        self.args = args
        self.coz = CozControl(self.sock)

    async def __aenter__(self):
        self.workload._delay_sampler = self.coz.global_delay_ns
        await self.workload.start(self.args)
        async with asyncio.timeout(20):
            while not os.path.exists(self.sock):
                assert self.workload.running, self.workload.tail()
                await asyncio.sleep(0.05)
        return self

    async def __aexit__(self, *exc):
        await self.coz.close()
        await self.workload.stop()

    async def run_for(self, seconds):
        start = asyncio.get_running_loop().time()
        await asyncio.sleep(seconds)
        return start, asyncio.get_running_loop().time()


async def test_manual_mode_runs_no_autonomous_experiments(tmp_path):
    async with Demo(tmp_path, ["--threads=4", "--time=60"]) as d:
        st = await d.coz.request("status")
        assert st["mode"] == "manual"
        assert st["experiment_active"] is False
        await asyncio.sleep(3)
    records = [json.loads(x) for x in Path(d.profile).read_text().splitlines()]
    assert not [r for r in records if r["type"] == "experiment"]


async def test_hot_lines_include_demo_lines(tmp_path):
    async with Demo(tmp_path, ["--threads=4", "--time=60"]) as d:
        await asyncio.sleep(2)
        hot = await d.coz.request("hot_lines", min_samples=10)
    names = [h["line"] for h in hot["lines"]]
    assert any(n.endswith(demo_line("P")) for n in names), names[:10]
    assert any(n.endswith(demo_line("S")) for n in names), names[:10]


async def test_speedup_inserts_virtual_delay(tmp_path):
    async with Demo(tmp_path, ["--threads=4", "--time=60"]) as d:
        await asyncio.sleep(1)
        await d.coz.request("start", line=demo_line("P"), kind="speedup", percent=50)
        await asyncio.sleep(2)
        r = await d.coz.request("stop")
    assert r["global_delay_ns_delta"] > 0
    assert r["slowdown_ns_delta"] == 0
    assert r["selected_samples"] > 0
    assert r["auto_stopped"] is False


async def test_slowdown_sleeps_without_virtual_delay(tmp_path):
    async with Demo(tmp_path, ["--threads=4", "--time=60"]) as d:
        await asyncio.sleep(1)
        await d.coz.request("start", line=demo_line("S"), kind="slowdown", percent=50)
        await asyncio.sleep(2)
        r = await d.coz.request("stop")
    assert r["slowdown_ns_delta"] > 0
    assert r["global_delay_ns_delta"] == 0


async def test_start_errors(tmp_path):
    async with Demo(tmp_path, ["--threads=2", "--time=60"]) as d:
        await asyncio.sleep(0.5)
        with pytest.raises(CozError, match="line not found"):
            await d.coz.request("start", line="nope.cc:1", kind="speedup", percent=5)
        await d.coz.request("start", line=demo_line("P"), kind="speedup", percent=5)
        with pytest.raises(CozError, match="already active"):
            await d.coz.request("start", line=demo_line("P"), kind="speedup", percent=5)
        await d.coz.request("stop")
        with pytest.raises(CozError, match="no experiment"):
            await d.coz.request("stop")


async def test_auto_stop(tmp_path):
    async with Demo(tmp_path, ["--threads=2", "--time=60"]) as d:
        await asyncio.sleep(0.5)
        await d.coz.request(
            "start", line=demo_line("P"), kind="slowdown", percent=10, max_duration_s=0.5
        )
        await asyncio.sleep(1.2)
        assert (await d.coz.request("status"))["experiment_active"] is False
        r = await d.coz.request("stop")
    assert r["auto_stopped"] is True


async def test_slowdown_of_serial_section_lowers_tps(tmp_path):
    async with Demo(tmp_path, ["--threads=4", "--time=60"]) as d:
        await asyncio.sleep(2)
        b0, b1 = await d.run_for(4)
        await d.coz.request("start", line=demo_line("S"), kind="slowdown", percent=50)
        e0, e1 = await d.run_for(4)
        await d.coz.request("stop")
        samples = d.workload.samples()
    base = window_tps(samples, b0, b1, corrected=False).raw_tps
    slow = window_tps(samples, e0, e1, corrected=False).raw_tps
    assert slow < base * 0.85, (base, slow)


async def test_mcp_run_experiment_on_demo(tmp_path):
    """Full stack: MCP tools -> CozControl -> libcoz in the demo."""
    from mcp import Client

    from coz_mcp.server import build_server

    d = Demo(tmp_path, [])
    server = build_server(d.coz, d.workload)
    async with Client(server) as c:
        d.workload._delay_sampler = d.coz.global_delay_ns
        r = await c.call_tool("start_workload", {"args": ["--threads=4", "--time=120"]})
        assert not r.is_error, r.content
        async with asyncio.timeout(20):
            while not os.path.exists(d.sock):
                await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        r = await c.call_tool("run_experiment", {
            "line": demo_line("S"), "kind": "slowdown", "percent": 50,
            "duration_s": 3, "repeats": 2, "warmup_s": 1, "cooldown_s": 0})
        assert not r.is_error, r.content
        s = r.structured_content["summary"]
        assert s["delta_pct"] < -15, s
        r = await c.call_tool("stop_workload", {})
    await d.coz.close()


async def _latch_top(tmp_path, threads):
    from mcp import Client

    from coz_mcp.server import build_server

    tmp_path.mkdir(exist_ok=True)
    d = Demo(tmp_path, [])
    async with Client(build_server(d.coz, d.workload, latch_poll_s=0)) as c:
        r = await c.call_tool("start_workload", {
            "args": [f"--threads={threads}", "--lock=spin-hooked", "--time=60"]})
        assert not r.is_error, r.content
        async with asyncio.timeout(20):
            while not os.path.exists(d.sock):
                await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        assert not (await c.call_tool("latch_stats_enable", {"on": True})).is_error
        hold = (await c.call_tool("latch_top", {"metric": "hold", "window_s": 2})).structured_content
        wait = (await c.call_tool("latch_top", {"metric": "wait", "window_s": 2})).structured_content
        tps = (await c.call_tool("workload_tps", {"last_s": 2})).structured_content["mean_tps"]
        await c.call_tool("stop_workload", {})
    await d.coz.close()
    return hold, wait, tps


async def test_latch_top_on_demo(tmp_path):
    hold, wait, tps = await _latch_top(tmp_path / "t4", 4)
    top = hold["latches"][0]
    assert top["name"] == "demo_lock"
    assert "demo.cc:" in top["site"]
    assert abs(top["count_per_s"] - tps) / tps < 0.2, (top, tps)
    assert 500 < top["avg_ns"] < 100_000, top
    _, wait1, _ = await _latch_top(tmp_path / "t1", 1)
    assert wait["latches"][0]["avg_ns"] > 2 * wait1["latches"][0]["avg_ns"], (wait, wait1)
