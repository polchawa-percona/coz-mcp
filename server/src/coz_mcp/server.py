"""coz-mcp: MCP server driving Coz causal-profiling experiments at runtime.

MCP concepts used here (see docs/architecture.md for a walkthrough):

- Tools: actions the model can take (start a benchmark, run an experiment).
  Typed Python signatures become the JSON input schema; returned dicts become
  structured tool results. A ToolError becomes an error result the model sees.
- Progress notifications: run_experiment reports progress while it runs.
- Resources: read-only data addressed by URI (the experiment log, hot lines).
- Subscriptions: clients can listen for "resource updated" events.
- Prompts: a reusable instruction template (find_bottleneck).
- Lifespan: setup/cleanup around the whole server run (kill the benchmark).
"""

from __future__ import annotations

import asyncio
import contextlib

import anyio
import json
import statistics
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import ResourceUpdated

from .control import CozError, CozUnavailable
from .latch import rank_latches
from .metrics import window_tps
from .stats import compare
from .workload import WorkloadError

EXPERIMENTS_URI = "coz://experiments"
HOT_LINES_URI = "coz://hot-lines"
LATCHES_URI = "coz://latches/top"

INSTRUCTIONS = """\
Drive Coz causal-profiling experiments on a running program under load.
Typical flow: start_workload -> wait for stable TPS (workload_tps) -> hot_lines
-> run_experiment on candidate lines (slowdown shows whether a line matters,
speedup predicts the gain from optimizing it) -> compare delta_pct and its
confidence interval. Only one experiment runs at a time.
For lock contention: latch_stats_enable, then latch_top ranks mutex/rw-latch
acquire sites by wait or hold time (needs latch hooks in the target).
"""


class App:
    """State shared by all tools of one server instance."""

    def __init__(self, coz: Any, workload: Any, bus: InMemorySubscriptionBus) -> None:
        self.coz = coz
        self.workload = workload
        self.bus = bus
        self.experiments: list[dict[str, Any]] = []
        self.hot_lines: dict[str, Any] = {"lines": []}
        self.latches: dict[str, Any] = {"wait": None, "hold": None}
        self.latch_enabled = False
        self.experiment_lock = asyncio.Lock()

    async def coz_request(self, cmd: str, **args: Any) -> dict[str, Any]:
        try:
            return await self.coz.request(cmd, **args)
        except (CozError, CozUnavailable) as e:
            raise ToolError(str(e)) from e

    async def publish(self, uri: str) -> None:
        await self.bus.publish(ResourceUpdated(uri=uri))


def _diff_hot_lines(before: dict, after: dict) -> tuple[int, list[dict]]:
    old = {x["line"]: x["samples"] for x in before["lines"]}
    total = after["total_samples"] - before["total_samples"]
    lines = []
    for x in after["lines"]:
        d = x["samples"] - old.get(x["line"], 0)
        if d > 0:
            lines.append({"line": x["line"], "samples": d})
    lines.sort(key=lambda x: -x["samples"])
    return total, lines


def build_server(coz: Any, workload: Any, latch_poll_s: float = 5.0) -> MCPServer:
    """Create the MCP server. coz: CozControl-like; workload: Workload-like."""
    bus = InMemorySubscriptionBus()
    app = App(coz, workload, bus)

    async def latch_poller() -> None:
        """Refresh coz://latches/top while latch stats are enabled."""
        while True:
            await asyncio.sleep(latch_poll_s)
            if not app.latch_enabled:
                continue
            try:
                before = await app.coz.request("latch_stats_snapshot")
                await asyncio.sleep(latch_poll_s)
                after = await app.coz.request("latch_stats_snapshot")
            except Exception:  # never let the poller die
                continue
            for metric in ("wait", "hold"):
                app.latches[metric] = rank_latches(before, after, metric=metric)
            await app.publish(LATCHES_URI)

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[App]:
        poller = asyncio.create_task(latch_poller()) if latch_poll_s > 0 else None
        try:
            yield app
        finally:
            if poller:
                poller.cancel()
            # Never leave a benchmark running or a target slowed down.
            with contextlib.suppress(Exception):
                await app.coz.request("stop")  # errors if nothing is running
            with contextlib.suppress(Exception):
                await app.workload.stop()

    mcp = MCPServer("coz-mcp", instructions=INSTRUCTIONS, lifespan=lifespan,
                    subscriptions=bus)

    # ----------------------------------------------------------------- tools

    @mcp.tool()
    async def coz_status() -> dict[str, Any]:
        """State of the profiled target (Coz) and of the benchmark workload."""
        try:
            coz_state: Any = await app.coz.request("status")
        except (CozError, CozUnavailable) as e:
            coz_state = {"error": str(e)}
        wl = app.workload
        last = wl.samples(last_s=5)
        return {
            "coz": coz_state,
            "workload": {
                "name": wl.config.name,
                "running": wl.running,
                "args": wl.args,
                "exit_code": wl.exit_code,
                "recent_tps": statistics.fmean(s.rate for s in last) if last else None,
                "output_tail": wl.tail(5),
            },
        }

    @mcp.tool()
    async def start_workload(args: list[str] | None = None) -> dict[str, Any]:
        """Start the configured benchmark in the background.

        args are appended to the configured command (e.g. ["--threads=64",
        "--time=600"]). The binary cannot be changed and --rate is rejected
        (open-loop load makes corrected throughput meaningless).
        """
        try:
            cmd = await app.workload.start(args or [])
        except WorkloadError as e:
            raise ToolError(str(e)) from e
        return {"command": cmd}

    @mcp.tool()
    async def stop_workload() -> dict[str, Any]:
        """Stop the benchmark."""
        await app.workload.stop()
        return {"running": app.workload.running, "exit_code": app.workload.exit_code}

    @mcp.tool()
    async def workload_tps(last_s: float = 10) -> dict[str, Any]:
        """Throughput reported by the benchmark over the last last_s seconds."""
        s = app.workload.samples(last_s=last_s)
        now = time.monotonic()
        return {
            "running": app.workload.running,
            "mean_tps": statistics.fmean(x.rate for x in s) if s else None,
            "series": [{"age_s": round(now - x.t_end, 3), "tps": x.rate} for x in s],
        }

    @mcp.tool()
    async def hot_lines(window_s: float = 5, limit: int = 20,
                        path_filter: str | None = None) -> dict[str, Any]:
        """Source lines where the target spent CPU during the last window_s seconds.

        These are candidates for run_experiment. Counts come from Coz's perf
        sampling (first in-scope line on the call stack). path_filter keeps only
        lines whose path contains the given substring.
        """
        before = await app.coz_request("hot_lines", min_samples=1)
        await asyncio.sleep(window_s)
        after = await app.coz_request("hot_lines", min_samples=1)
        total, lines = _diff_hot_lines(before, after)
        if path_filter:
            lines = [x for x in lines if path_filter in x["line"]]
        for x in lines:
            x["share_pct"] = round(100.0 * x["samples"] / total, 2) if total else 0.0
        app.hot_lines = {"window_s": window_s, "total_samples": total,
                         "lines": lines[:limit]}
        await app.publish(HOT_LINES_URI)
        return app.hot_lines

    @mcp.tool()
    async def run_experiment(
        line: str,
        kind: Literal["speedup", "slowdown"],
        percent: int,
        ctx: Context,
        duration_s: float = 20,
        repeats: int = 3,
        warmup_s: float = 3,
        cooldown_s: float = 1,
    ) -> dict[str, Any]:
        """Measure how one source line affects benchmark throughput.

        line is "file:line" (path suffix is enough, e.g. "lock0lock.cc:812").
        kind=slowdown really makes the line percent% slower: a large TPS drop
        means the line is on the critical path. kind=speedup virtually makes
        it percent% faster (Coz): the corrected TPS predicts the gain of
        optimizing it. Runs `repeats` pairs of (0% baseline, target) on the
        same line, each with warmup_s settling time and duration_s measurement,
        and returns per-run TPS plus delta_pct with a 95% confidence interval.
        """
        if not 0 <= percent <= 100:
            raise ToolError("percent must be 0..100")
        if repeats < 1 or duration_s <= 0 or warmup_s < 0:
            raise ToolError("repeats >= 1, duration_s > 0, warmup_s >= 0 required")
        if not app.workload.running:
            raise ToolError("no workload running; call start_workload first")
        if app.experiment_lock.locked():
            raise ToolError("an experiment is already running; wait for it to finish")

        async with app.experiment_lock:
            runs: list[dict[str, Any]] = []
            plan = [p for _ in range(repeats) for p in (0, percent)]
            for i, pct in enumerate(plan):
                await ctx.report_progress(
                    i, len(plan), f"run {i + 1}/{len(plan)}: {kind} {pct}% on {line}")
                runs.append(await _one_run(line, kind, pct, duration_s, warmup_s))
                if not app.workload.running:
                    raise ToolError("workload stopped during the experiment: "
                                    + " | ".join(app.workload.tail(5)))
                if cooldown_s and i + 1 < len(plan):
                    await asyncio.sleep(cooldown_s)
            await ctx.report_progress(len(plan), len(plan), "done")

        metric = "corrected_tps" if kind == "speedup" else "raw_tps"
        base = [r[metric] for r in runs if r["percent"] == 0 and r[metric] is not None]
        target = [r[metric] for r in runs
                  if r["percent"] == percent and r[metric] is not None]
        if not base or not target:
            raise ToolError("no complete benchmark intervals inside the measurement "
                            "windows; increase duration_s")
        result = {
            "line": line,
            "kind": kind,
            "percent": percent,
            "metric": metric,
            "summary": compare(base, target),
            "runs": runs,
            "caveats": _caveats(kind, runs),
        }
        app.experiments.append(result)
        await app.publish(EXPERIMENTS_URI)
        return result

    async def _one_run(line: str, kind: str, pct: int, duration_s: float,
                       warmup_s: float) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        started = await app.coz_request(
            "start", line=line, kind=kind, percent=pct,
            max_duration_s=warmup_s + duration_s + 30)
        stopped: dict[str, Any] = {}
        try:
            await asyncio.sleep(warmup_s)
            t0 = loop.time()
            await asyncio.sleep(duration_s)
            t1 = loop.time()
        finally:
            # Stop even if the tool call is being cancelled: the SDK cancels
            # through anyio cancel scopes, which would also cancel this await
            # and leave the target slowed down until Coz's auto-stop.
            with anyio.CancelScope(shield=True), anyio.move_on_after(15):
                stopped = await app.coz_request("stop")
        w = window_tps(app.workload.samples(), t0, t1, corrected=(kind == "speedup"))
        return {
            "percent": pct,
            "raw_tps": w.raw_tps,
            "corrected_tps": w.corrected_tps,
            "intervals": w.intervals,
            "clamped_intervals": w.clamped_intervals,
            "uncorrected_intervals": w.uncorrected_intervals,
            "selected_samples": stopped.get("selected_samples"),
            "global_delay_ns": stopped.get("global_delay_ns_delta"),
            "slowdown_ns": stopped.get("slowdown_ns_delta"),
            "warning": started.get("warning"),
        }

    def _caveats(kind: str, runs: list[dict[str, Any]]) -> list[str]:
        out = []
        if kind == "speedup":
            out.append("Speedup is virtual: TPS is corrected for Coz delays; the "
                       "benchmark client runs outside virtual time (keep it on "
                       "separate CPUs).")
        if any(r["selected_samples"] == 0 for r in runs if r["percent"] > 0):
            out.append("The line got no samples during a target run, so the "
                       "experiment had no effect. Pick a line from hot_lines.")
        if any(r["uncorrected_intervals"] for r in runs) and kind == "speedup":
            out.append("Some intervals could not be corrected (missing delay "
                       "samples).")
        return out

    @mcp.tool()
    async def latch_stats_enable(on: bool = True) -> dict[str, Any]:
        """Turn per-site latch wait/hold statistics on or off in the target.

        Needs a target built with latch hooks (COZ_LATCH_* in coz.h, e.g.
        Percona Server built with -DWITH_COZ=ON). Costs a few tens of ns per
        latch operation while on.
        """
        r = await app.coz_request("latch_stats", enable=on)
        app.latch_enabled = on
        return r

    @mcp.tool()
    async def latch_stats_reset() -> dict[str, Any]:
        """Zero all latch statistics (including the max values)."""
        return await app.coz_request("latch_stats_reset")

    @mcp.tool()
    async def latch_top(metric: Literal["wait", "hold"] = "hold", window_s: float = 5,
                        limit: int = 20, latch: str | None = None,
                        path_filter: str | None = None) -> dict[str, Any]:
        """Rank mutex/rw-latch acquire sites by time over the last window_s seconds.

        metric=wait: time threads spent waiting to acquire at that site
        (contention); metric=hold: time the latch was held after being
        acquired there (critical section length). Per site: acquisitions/s,
        total ms per second, average, p50/p99 (log2 histogram upper bounds) and
        max since reset. latch filters by latch name, path_filter by file.
        """
        if not app.latch_enabled:
            raise ToolError("latch statistics are off; call latch_stats_enable first")
        before = await app.coz_request("latch_stats_snapshot")
        await asyncio.sleep(window_s)
        after = await app.coz_request("latch_stats_snapshot")
        r = rank_latches(before, after, metric=metric, limit=limit, latch=latch,
                         path_filter=path_filter)
        caveats = ["Hold time covers the whole critical section, not only the "
                   "acquire line; use hot_lines to find the expensive lines inside."]
        if app.experiment_lock.locked():
            caveats.append("An experiment is running: times include injected "
                           "slowdown or Coz virtual delays.")
        if r["reset_detected"]:
            caveats.append("Counters were reset during the window.")
        r["caveats"] = caveats
        app.latches[metric] = r
        await app.publish(LATCHES_URI)
        return r

    # ------------------------------------------------------------- resources

    @mcp.resource(EXPERIMENTS_URI, name="experiments", mime_type="application/json",
                  description="All run_experiment results of this session.")
    def experiments_resource() -> str:
        return json.dumps(app.experiments, indent=1)

    @mcp.resource(HOT_LINES_URI, name="hot-lines", mime_type="application/json",
                  description="Hot lines from the latest hot_lines call.")
    def hot_lines_resource() -> str:
        return json.dumps(app.hot_lines, indent=1)

    @mcp.resource(LATCHES_URI, name="latches-top", mime_type="application/json",
                  description="Latest latch wait and hold rankings (refreshed "
                              "in the background while latch stats are on).")
    def latches_resource() -> str:
        return json.dumps(app.latches, indent=1)

    # --------------------------------------------------------------- prompts

    @mcp.prompt()
    def find_bottleneck(candidates: int = 5, workload_args: str = "") -> str:
        """Plan for finding which code lines limit benchmark throughput."""
        return f"""\
Find which source lines limit the throughput of the benchmark.

1. Call coz_status. If the workload is not running, call start_workload
   (args: {workload_args or "none"}) and poll workload_tps until TPS is stable.
2. Call hot_lines (window_s=10) and pick the top {candidates} lines in project
   code (skip system headers).
3. For each candidate call run_experiment(kind="slowdown", percent=50).
   Lines whose delta_pct confidence interval includes 0 are not on the
   critical path; drop them.
4. For the remaining lines call run_experiment(kind="speedup") with percent
   20 and 50 to predict the gain of optimizing them.
5. Report a ranked table: line, slowdown delta_pct, predicted speedup gain,
   and a short explanation of what the code does (read the source).
"""

    return mcp


def main() -> None:
    from .config import from_env
    from .control import CozControl
    from .workload import Workload

    cfg = from_env()
    coz = CozControl(cfg.control_socket)
    workload = Workload(cfg.workload, delay_sampler=coz.global_delay_ns)
    try:
        build_server(coz, workload).run()
    finally:
        workload.kill_now()


if __name__ == "__main__":
    main()
