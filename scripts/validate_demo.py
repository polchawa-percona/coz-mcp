"""Validate coz-mcp on the demo workload against real code changes.

Usage:
  cd server && uv run python ../scripts/validate_demo.py [--build DIR]
      [--cpus 8-13] [--duration 6] [--repeats 3] [--env NAME=VALUE ...]

Each case runs one experiment through the MCP tools (run_experiment on the
demo under libcoz) and the matching ground truth: the demo without Coz,
alternating the unchanged program and a variant whose work on that line
really is longer/shorter (--serial-work-pct / --parallel-work-pct). Both
deltas are measured in the same session with the demo pinned to the same
CPUs, so machine drift cancels out.

The real change is measured twice: on the plain program, and on the same
program with libcoz loaded but no experiment running ("under libcoz"). Coz can
only predict the program it runs in. On some hosts the per-thread hardware
perf event that libcoz samples with changes the program itself: on an AMD
Zen5 host it cut the real gain of the speedup cases from about +41% to about
+29% (see .requirements/*_speedup_underprediction/FINDINGS.md).

A case passes when |coz delta - real delta| <= max(5 points, |real| / 3)
(tolerance is an estimate, see REQUIREMENTS R13). The exit code uses the real
change of the plain program; the verdict against the program under libcoz is
printed for information.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import statistics
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from mcp import Client

from coz_mcp.config import coz_env, demo_workload
from coz_mcp.control import CozControl
from coz_mcp.server import build_server
from coz_mcp.stats import compare
from coz_mcp.workload import Workload, _child_setup

REPO = Path(__file__).resolve().parents[1]


def demo_line(tag: str) -> str:
    for n, text in enumerate((REPO / "demo/demo.cc").read_text().splitlines(), 1):
        if re.search(rf"//\s*COZ-LINE:\s*{tag}\b", text):
            return f"demo.cc:{n}"
    raise SystemExit(f"no COZ-LINE {tag}")


@dataclass
class Case:
    name: str
    lock: str
    tag: str  # COZ-LINE tag: S (serial) or P (parallel)
    kind: str  # slowdown | speedup
    percent: int
    checked: bool = True

    @property
    def truth_flag(self) -> str:
        work = "serial" if self.tag == "S" else "parallel"
        pct = 100 + self.percent if self.kind == "slowdown" else 100 - self.percent
        return f"--{work}-work-pct={pct}"


CASES = [
    Case("slowdown S, pthread", "pthread", "S", "slowdown", 50),
    Case("slowdown P, pthread", "pthread", "P", "slowdown", 50),
    Case("speedup S, pthread", "pthread", "S", "speedup", 30),
    Case("slowdown S, spin-hooked", "spin-hooked", "S", "slowdown", 50),
    Case("speedup S, spin-hooked", "spin-hooked", "S", "speedup", 30),
    Case("slowdown S, spin (no hooks)", "spin", "S", "slowdown", 50, checked=False),
]


class Runner:
    def __init__(self, a: argparse.Namespace) -> None:
        self.build = Path(a.build)
        self.cpus = a.cpus
        self.duration = a.duration
        self.repeats = a.repeats
        self.threads = f"--threads={a.threads}"
        self.extra_env = dict(kv.split("=", 1) for kv in a.env)

    async def coz_delta(self, c: Case) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            sock = os.path.join(tmp, "coz.sock")
            coz = CozControl(sock)
            cfg = demo_workload(self.build, sock, os.path.join(tmp, "p.coz"),
                                cpus=self.cpus, extra_env=self.extra_env)
            wl = Workload(cfg, delay_sampler=coz.global_delay_ns)
            async with Client(build_server(coz, wl)) as cl:
                r = await cl.call_tool("start_workload", {
                    "args": [self.threads, f"--lock={c.lock}", "--time=3600"]})
                assert not r.is_error, r.content
                async with asyncio.timeout(30):
                    while not os.path.exists(sock):
                        await asyncio.sleep(0.05)
                await asyncio.sleep(2)
                r = await cl.call_tool("run_experiment", {
                    "line": demo_line(c.tag), "kind": c.kind, "percent": c.percent,
                    "duration_s": self.duration, "repeats": self.repeats,
                    "warmup_s": 1, "cooldown_s": 0.5})
                assert not r.is_error, r.content
                await cl.call_tool("stop_workload", {})
            await coz.close()
            return r.structured_content["summary"]

    async def plain_tps(self, args: list[str], under_coz: bool = False) -> float:
        cmd = [str(self.build / "demo/demo"), self.threads, *args,
               f"--time={int(self.duration) + 1}"]
        with tempfile.TemporaryDirectory() as tmp:
            env = None
            if under_coz:
                # libcoz loaded, no experiment: nobody connects to the socket.
                env = {**os.environ,
                       **coz_env(self.build, os.path.join(tmp, "coz.sock"),
                                 os.path.join(tmp, "p.coz")),
                       **self.extra_env}
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                env=env, preexec_fn=_child_setup(self.cpus))
            out, _ = await proc.communicate()
        rates = [float(m) for m in re.findall(r"tps: ([0-9.]+)", out.decode())]
        return statistics.fmean(rates[1:])  # skip the startup second

    async def real_delta(self, c: Case, under_coz: bool = False) -> dict:
        base, variant = [], []
        for _ in range(self.repeats):
            base.append(await self.plain_tps([f"--lock={c.lock}"], under_coz))
            variant.append(await self.plain_tps([f"--lock={c.lock}", c.truth_flag],
                                                 under_coz))
        return compare(base, variant)


def fmt(s: dict) -> str:
    ci = s["delta_pct_ci95"]
    ci_txt = f"[{ci[0]:+6.1f},{ci[1]:+6.1f}]" if ci else "n/a"
    return f"{s['delta_pct']:+6.1f}% {ci_txt}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=os.environ.get(
        "COZ_MCP_BUILD_DIR", str(Path.home() / "builds/coz-mcp-release")))
    ap.add_argument("--cpus", default="8-13", help="taskset list for the demo")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--duration", type=float, default=6)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--env", action="append", default=[],
                    help="extra environment for libcoz, e.g. COZ_LOCK_AWARE_DELAYS=0")
    ap.add_argument("--only", help="run only cases whose name contains this text")
    a = ap.parse_args()
    r = Runner(a)

    print(f"{'case':30s} {'coz-mcp':>24s} {'real, under libcoz':>24s} "
          f"{'real, plain':>24s}  verdict (libcoz/plain)")
    ok = True
    for c in CASES:
        if a.only and a.only not in c.name:
            continue
        coz = await r.coz_delta(c)
        real_coz = await r.real_delta(c, under_coz=True)
        real = await r.real_delta(c)

        def verdict(truth: dict) -> tuple[bool, str]:
            diff = abs(coz["delta_pct"] - truth["delta_pct"])
            passed = diff <= max(5.0, abs(truth["delta_pct"]) / 3)
            return passed, ("PASS" if passed else "FAIL") if c.checked else "info"

        _, v_coz = verdict(real_coz)
        passed, v_plain = verdict(real)
        if c.checked:
            ok &= passed
        print(f"{c.name:30s} {fmt(coz):>24s} {fmt(real_coz):>24s} {fmt(real):>24s}  "
              f"{v_coz}/{v_plain} ({c.truth_flag})", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
