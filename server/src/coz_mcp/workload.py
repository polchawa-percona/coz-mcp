"""Background benchmark process (sysbench or demo) and its TPS time series."""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence

from .metrics import Sample
from .report import parse_report_line

DelaySampler = Callable[[], Awaitable[int]]


class WorkloadError(Exception):
    pass


@dataclass(frozen=True)
class WorkloadConfig:
    """A preset fixed by the server operator; tools can only append args."""

    name: str
    binary: str
    base_args: list[str] = field(default_factory=list)
    # Args that make the benchmark print one report line per second.
    report_args: list[str] = field(default_factory=list)
    cpus: str | None = None  # taskset CPU list for the benchmark client
    env: dict[str, str] = field(default_factory=dict)  # added to os.environ


def _check_args(args: Sequence[str]) -> None:
    for a in args:
        if a == "--rate" or a.startswith("--rate="):
            raise WorkloadError(
                "--rate is not allowed: open-loop load makes Coz-corrected "
                "throughput meaningless"
            )


def parse_cpus(spec: str) -> set[int]:
    """CPU list like "8-13,20" -> {8, 9, 10, 11, 12, 13, 20}."""
    cpus: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-")
            cpus.update(range(int(lo), int(hi) + 1))
        elif part:
            cpus.add(int(part))
    return cpus


def _pin(spec: str) -> Callable[[], None]:
    cpus = parse_cpus(spec)
    return lambda: os.sched_setaffinity(0, cpus)


class Workload:
    def __init__(
        self,
        config: WorkloadConfig,
        delay_sampler: DelaySampler | None = None,
        max_samples: int = 100_000,
        max_lines: int = 200,
    ) -> None:
        self.config = config
        self._delay_sampler = delay_sampler
        self._samples: collections.deque[Sample] = collections.deque(maxlen=max_samples)
        self._lines: collections.deque[str] = collections.deque(maxlen=max_lines)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._last_t_s = 0
        self.args: list[str] = []
        self.started_at: float | None = None
        self.exit_code: int | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def command(self, extra_args: Sequence[str]) -> list[str]:
        return [self.config.binary, *self.config.base_args, *extra_args,
                *self.config.report_args]

    async def start(self, extra_args: Sequence[str]) -> list[str]:
        _check_args(extra_args)
        if self.running:
            raise WorkloadError("workload already running; stop it first")
        cmd = self.command(extra_args)
        self._samples.clear()
        self._lines.clear()
        self._last_t_s = 0
        self.exit_code = None
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **self.config.env} if self.config.env else None,
            # Pin in the child, not via taskset: taskset would inherit LD_PRELOAD.
            preexec_fn=_pin(self.config.cpus) if self.config.cpus else None,
        )
        self.args = list(extra_args)
        self.started_at = time.monotonic()
        self._reader = asyncio.create_task(self._read(self._proc))
        return cmd

    async def _sample_delay(self) -> int | None:
        if self._delay_sampler is None:
            return None
        try:
            return await self._delay_sampler()
        except Exception:
            return None

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            now = time.monotonic()
            line = raw.decode(errors="replace").rstrip("\n")
            self._lines.append(line)
            rep = parse_report_line(line)
            if rep is None:
                continue
            interval = float(rep.t_s - self._last_t_s) if rep.t_s > self._last_t_s else 1.0
            self._last_t_s = rep.t_s
            delay = await self._sample_delay()
            self._samples.append(
                Sample(t_end=now, interval_s=interval, rate=rep.rate, global_delay_ns=delay)
            )
        self.exit_code = await proc.wait()

    async def stop(self, timeout: float = 5.0) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await self._reader
        self.exit_code = proc.returncode

    def kill_now(self) -> None:
        """Synchronous kill for interpreter shutdown."""
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._proc.kill()

    def samples(self, last_s: float | None = None) -> list[Sample]:
        s = list(self._samples)
        if last_s is None or not s:
            return s
        cutoff = s[-1].t_end - last_s
        return [x for x in s if x.t_end >= cutoff]

    def tail(self, n: int = 20) -> list[str]:
        return list(self._lines)[-n:]
