"""Validate coz-mcp on the demo workload through the real MCP tools.

Usage:
  cd server && uv run python ../scripts/validate_demo.py [--build DIR] [--quick]

Checks (R13, R16/R17 of the requirements):
  1. pthread lock: slowdown of the serial line S by 50% lowers TPS clearly.
  2. pthread lock: slowdown of the parallel line P by 50% lowers TPS less than S.
  3. spin-hooked lock: slowdown of S has an effect close to the pthread case.
  4. spin lock without hooks: slowdown of S has a clearly smaller effect.
  Reported without pass/fail:
  5. speedup of S by 30% (Coz prediction) vs the real --serial-work-pct=70.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import tempfile
from pathlib import Path

from mcp import Client

from coz_mcp.config import demo_workload
from coz_mcp.control import CozControl
from coz_mcp.server import build_server
from coz_mcp.workload import Workload

REPO = Path(__file__).resolve().parents[1]


def demo_line(tag: str) -> str:
    for n, text in enumerate((REPO / "demo/demo.cc").read_text().splitlines(), 1):
        if re.search(rf"//\s*COZ-LINE:\s*{tag}\b", text):
            return f"demo.cc:{n}"
    raise SystemExit(f"no COZ-LINE {tag}")


async def experiment(build: Path, demo_args: list[str], line: str, kind: str,
                     percent: int, duration: float, repeats: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "coz.sock")
        coz = CozControl(sock)
        wl = Workload(demo_workload(build, sock, os.path.join(tmp, "p.coz")),
                      delay_sampler=coz.global_delay_ns)
        async with Client(build_server(coz, wl)) as c:
            r = await c.call_tool("start_workload", {"args": demo_args + ["--time=3600"]})
            assert not r.is_error, r.content
            async with asyncio.timeout(30):
                while not os.path.exists(sock):
                    await asyncio.sleep(0.05)
            await asyncio.sleep(2)
            r = await c.call_tool("run_experiment", {
                "line": line, "kind": kind, "percent": percent,
                "duration_s": duration, "repeats": repeats, "warmup_s": 1,
                "cooldown_s": 0.5})
            assert not r.is_error, r.content
            await c.call_tool("stop_workload", {})
        await coz.close()
        return r.structured_content["summary"]


async def plain_tps(build: Path, demo_args: list[str], seconds: int) -> float:
    proc = await asyncio.create_subprocess_exec(
        str(build / "demo/demo"), *demo_args, f"--time={seconds}",
        stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    rates = [float(m) for m in re.findall(r"tps: ([0-9.]+)", out.decode())]
    return statistics.fmean(rates[1:])  # skip the first (startup) second


def fmt(s: dict) -> str:
    ci = s["delta_pct_ci95"]
    ci_txt = f"[{ci[0]:+.1f}, {ci[1]:+.1f}]" if ci else "n/a"
    return f"{s['delta_pct']:+6.1f}% CI95 {ci_txt}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=os.environ.get(
        "COZ_MCP_BUILD_DIR", str(Path.home() / "builds/coz-mcp-release")))
    ap.add_argument("--quick", action="store_true", help="shorter runs")
    a = ap.parse_args()
    build = Path(a.build)
    duration, repeats = (3, 2) if a.quick else (6, 3)
    threads = ["--threads=4"]
    S, P = demo_line("S"), demo_line("P")

    results = {}
    for lock in ("pthread", "spin-hooked", "spin"):
        args = threads + [f"--lock={lock}"]
        results[lock, "S"] = await experiment(build, args, S, "slowdown", 50, duration, repeats)
        print(f"slowdown S 50% lock={lock:12s} {fmt(results[lock, 'S'])}", flush=True)
    results["pthread", "P"] = await experiment(
        build, threads + ["--lock=pthread"], P, "slowdown", 50, duration, repeats)
    print(f"slowdown P 50% lock=pthread      {fmt(results['pthread', 'P'])}", flush=True)

    pred = await experiment(build, threads + ["--lock=pthread"], S, "speedup", 30,
                            duration, repeats)
    base = await plain_tps(build, threads + ["--lock=pthread"], duration + 1)
    real = await plain_tps(build, threads + ["--lock=pthread", "--serial-work-pct=70"],
                           duration + 1)
    real_gain = (real - base) / base * 100
    print(f"speedup S 30%: predicted {fmt(pred)}, real {real_gain:+.1f}% "
          f"({base:.0f} -> {real:.0f} tps without coz)", flush=True)

    d = {k: v["delta_pct"] for k, v in results.items()}
    checks = [
        ("pthread: slowdown S drops TPS >= 15%", d["pthread", "S"] <= -15),
        ("pthread: slowdown P hurts less than S", d["pthread", "P"] > d["pthread", "S"]),
        ("spin-hooked: slowdown S within 1/3 of pthread effect",
         abs(d["spin-hooked", "S"] - d["pthread", "S"]) <= abs(d["pthread", "S"]) / 3),
        ("spin (no hooks): slowdown S effect smaller than spin-hooked",
         d["spin", "S"] > d["spin-hooked", "S"]),
    ]
    ok = True
    for name, passed in checks:
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
