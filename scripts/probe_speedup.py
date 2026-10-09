"""Diagnose virtual speedup accuracy on the demo (PS-11508).

For one demo configuration, measures in the same session:
  - plain demo TPS (no libcoz) for the unchanged program and the real change,
  - demo under libcoz with no experiment (idle) and per-run details of a
    speedup experiment (0% baseline and target runs): raw TPS, corrected TPS,
    virtual delay fraction x = global_delay / run wall time, selected samples,
  - hot_lines sample rate (samples per second per line) while idle.

Usage:
  cd server && uv run python ../scripts/probe_speedup.py --threads 1 \
      [--lock pthread] [--tag S] [--percent 30] [--env NAME=VALUE ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from pathlib import Path

from mcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_demo import demo_line  # noqa: E402

from coz_mcp.config import coz_env, demo_workload  # noqa: E402
from coz_mcp.control import CozControl  # noqa: E402
from coz_mcp.metrics import window_tps  # noqa: E402
from coz_mcp.server import build_server  # noqa: E402
from coz_mcp.workload import Workload, _child_setup  # noqa: E402


async def plain_tps(build: Path, cpus: str, args: list[str], secs: int,
                    env: dict[str, str] | None = None) -> float:
    """TPS of the demo without libcoz, or with libcoz loaded but idle (env)."""
    cmd = [str(build / "demo/demo"), *args, f"--time={secs + 1}"]
    with tempfile.TemporaryDirectory() as tmp:
        full_env = None
        if env is not None:
            full_env = {**os.environ, **coz_env(build, os.path.join(tmp, "s"),
                                                os.path.join(tmp, "p.coz")), **env}
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=full_env, preexec_fn=_child_setup(cpus))
        out, _ = await proc.communicate()
    return _mean_tps(out)


def _mean_tps(out: bytes) -> float:
    rates = [float(x.split("tps: ")[1].split()[0]) for x in out.decode().splitlines()
             if "tps: " in x]
    return statistics.fmean(rates[1:])


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=os.environ.get(
        "COZ_MCP_BUILD_DIR", str(Path.home() / "builds/coz-mcp-release")))
    ap.add_argument("--cpus", default="8-13")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--lock", default="pthread")
    ap.add_argument("--tag", default="S")
    ap.add_argument("--percent", type=int, default=30)
    ap.add_argument("--duration", type=float, default=5)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--extra", action="append", default=[],
                    help="extra demo argument, e.g. --extra=--serial-work=2000")
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--no-real", action="store_true")
    a = ap.parse_args()
    build = Path(a.build)
    demo_args = [f"--threads={a.threads}", f"--lock={a.lock}", *a.extra]
    work = "serial" if a.tag == "S" else "parallel"
    out: dict = {"config": vars(a)}

    env = dict(kv.split("=", 1) for kv in a.env)
    if not a.no_real:
        base, var, zero, ibase, ivar = [], [], [], [], []
        flag = f"--{work}-work-pct={100 - a.percent}"
        for _ in range(a.repeats):
            # The same program with libcoz loaded but no experiment running.
            ibase.append(await plain_tps(build, a.cpus, demo_args, int(a.duration), env))
            ivar.append(await plain_tps(build, a.cpus, demo_args + [flag],
                                        int(a.duration), env))
            base.append(await plain_tps(build, a.cpus, demo_args, int(a.duration)))
            var.append(await plain_tps(build, a.cpus, demo_args + [
                f"--{work}-work-pct={100 - a.percent}"], int(a.duration)))
            zero.append(await plain_tps(build, a.cpus, demo_args + [
                f"--{work}-work-pct=0"], int(a.duration)))
        out["real"] = {"base": base, "variant": var, "line_removed": zero,
                       "gain_pct": 100 * (statistics.fmean(var) / statistics.fmean(base) - 1)}
        out["real_under_libcoz"] = {
            "base": ibase, "variant": ivar,
            "gain_pct": 100 * (statistics.fmean(ivar) / statistics.fmean(ibase) - 1)}
    out["loadavg_start"] = open("/proc/loadavg").read().split()[:3]
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "coz.sock")
        coz = CozControl(sock)
        cfg = demo_workload(build, sock, os.path.join(tmp, "p.coz"), cpus=a.cpus,
                            extra_env=env)
        wl = Workload(cfg, delay_sampler=coz.global_delay_ns)
        async with Client(build_server(coz, wl)) as cl:
            r = await cl.call_tool("start_workload", {"args": [*demo_args, "--time=3600"]})
            assert not r.is_error, r.content
            async with asyncio.timeout(30):
                while not os.path.exists(sock):
                    await asyncio.sleep(0.05)
            await asyncio.sleep(2)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            hl = await cl.call_tool("hot_lines", {"window_s": a.duration, "limit": 5})
            t1 = loop.time()
            out["idle_raw_tps"] = window_tps(wl.samples(), t0, t1, corrected=False).raw_tps
            out["hot_lines"] = hl.structured_content
            out["status"] = (await cl.call_tool("coz_status", {})).structured_content
            r = await cl.call_tool("run_experiment", {
                "line": demo_line(a.tag), "kind": "speedup", "percent": a.percent,
                "duration_s": a.duration, "repeats": a.repeats,
                "warmup_s": 1, "cooldown_s": 0.5})
            assert not r.is_error, r.content
            await cl.call_tool("stop_workload", {})
        await coz.close()
    res = r.structured_content
    out["summary"] = res["summary"]
    runs = res["runs"]
    for x in runs:
        x["x_delay_frac"] = (x["global_delay_ns"] or 0) / ((a.duration + 1) * 1e9)
    out["runs"] = runs
    b = [x for x in runs if x["percent"] == 0]
    t = [x for x in runs if x["percent"] > 0]
    out["raw_ratio_target_vs_base"] = (statistics.fmean(x["raw_tps"] for x in t)
                                       / statistics.fmean(x["raw_tps"] for x in b))
    out["raw_ratio_base0_vs_idle"] = statistics.fmean(x["raw_tps"] for x in b) / out["idle_raw_tps"]
    out["loadavg_end"] = open("/proc/loadavg").read().split()[:3]
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
