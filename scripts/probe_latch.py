"""Lock behaviour of the demo (spin-hooked) with no experiment, a 0% and a
speedup experiment, from latch statistics (PS-11508 diagnostics).

Usage:
  cd server && uv run python ../scripts/probe_latch.py [--extra=--serial-work-pct=70]
      [--env NAME=VALUE ...]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_demo import demo_line  # noqa: E402

from coz_mcp.config import demo_workload  # noqa: E402
from coz_mcp.control import CozControl  # noqa: E402
from coz_mcp.metrics import window_tps  # noqa: E402
from coz_mcp.workload import Workload  # noqa: E402


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default=str(Path.home() / "builds/coz-mcp-release"))
    ap.add_argument("--cpus", default="8-13")
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--percent", type=int, default=30)
    ap.add_argument("--duration", type=float, default=5)
    ap.add_argument("--extra", action="append", default=[])
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--arms", default="idle,0,P")
    a = ap.parse_args()
    env = dict(kv.split("=", 1) for kv in a.env)
    out = {"config": vars(a), "arms": []}
    with tempfile.TemporaryDirectory() as tmp:
        sock = os.path.join(tmp, "coz.sock")
        coz = CozControl(sock)
        cfg = demo_workload(a.build, sock, os.path.join(tmp, "p.coz"), cpus=a.cpus,
                            extra_env=env)
        wl = Workload(cfg, delay_sampler=coz.global_delay_ns)
        await wl.start([f"--threads={a.threads}", "--lock=spin-hooked", *a.extra,
                        "--time=3600"])
        try:
            async with asyncio.timeout(30):
                while not os.path.exists(sock):
                    await asyncio.sleep(0.05)
            await asyncio.sleep(2)
            await coz.request("latch_stats", enable=True)
            loop = asyncio.get_running_loop()
            for arm in a.arms.split(","):
                pct = None if arm == "idle" else (a.percent if arm == "P" else int(arm))
                if pct is not None:
                    await coz.request("start", line=demo_line("S"), kind="speedup",
                                      percent=pct, max_duration_s=60)
                await asyncio.sleep(1)
                await coz.request("latch_stats_reset")
                t0 = loop.time()
                await asyncio.sleep(a.duration)
                snap = await coz.request("latch_stats_snapshot")
                t1 = loop.time()
                stop = await coz.request("stop") if pct is not None else {}
                w = window_tps(wl.samples(), t0, t1, corrected=pct is not None)
                lat = [x for x in snap["latches"] if x["name"] == "demo_lock"][0]
                wall = (t1 - t0) * 1e9
                n = lat["hold_count"]
                out["arms"].append({
                    "arm": arm, "raw_tps": w.raw_tps, "corrected_tps": w.corrected_tps,
                    "acquires_per_s": n / (wall / 1e9),
                    "hold_us": lat["hold_ns"] / n / 1e3,
                    "wait_us": lat["wait_ns"] / lat["count"] / 1e3,
                    "busy": lat["hold_ns"] / wall,
                    "wait_hist": lat["wait_hist"], "hold_hist": lat["hold_hist"],
                    "global_delay_ms": stop.get("global_delay_ns_delta", 0) / 1e6,
                    "counters": stop.get("delay_counters"),
                })
                await asyncio.sleep(0.5)
        finally:
            await wl.stop()
            await coz.close()
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
