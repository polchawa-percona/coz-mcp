# REQUIREMENTS - virtual speedup under-prediction (PS-11508)

## As Is

- README validation (demo, 4 threads, speedup of the critical section S by
  30%): coz-mcp +29.9% vs real +40.9% (pthread), +30.5% vs +45.7% (hooked spin
  lock). Slowdowns match. An earlier run of the same case gave +35%.
- `profiler::add_delays` (`coz/libcoz/profiler.cpp`, Linux branch) credits a
  pause with the time actually slept (`waited`), including nanosleep overshoot
  (~50 us timer slack). The thread is then ahead and pushes the overshoot into
  the global delay; every other thread pauses for it and overshoots again.
- No visibility into how virtual delay was credited, paid or skipped.
- `scripts/validate_demo.py` compares predictions only with the plain program.

## Findings (details and tables in FINDINGS.md)

1. Overshoot propagation is a defect: on the 4-thread demo the global delay
   grows 5.7x faster than the sampled credit, ~180k pauses per 6 s run, the
   program runs at ~47% of its speed during the experiment, and the corrected
   TPS depends on host sleep latency (explains the README run-to-run spread).
2. The dominant cause of the gap to the plain program is an observer effect:
   any per-thread hardware perf event (ref-cycles, cycles, instructions; also
   `perf stat` without libcoz) slows the faster variant by 12-17% but the
   unchanged demo by ~3% on the AMD Zen5 host, so under libcoz the real gain
   is ~29-30%, not ~41-46%. Software clocks avoid it but attribute 6-12%
   instead of ~27% of samples to S.
3. Refuted: H1 (no pause inside a lock, skips consistent), H2 (cap never
   hit), H3 for ref-cycles (1-thread share 28.5% by samples and by removal),
   H4 (1-thread correction right), H5 (base and variant similarly lock-busy),
   H6 (smaller/smoother delays and spinning do not raise the prediction),
   H7 on the demo (0% experiment vs idle: 0.99-1.02).

## To Be

- Opt-in `COZ_PAUSE_OVERSHOOT=bank`: a pause credits only what was needed;
  the overshoot is banked per thread and pays that thread's next delays. The
  global delay of a speedup experiment then equals the sampled credit.
  Default stays `propagate` (upstream) because bank predicted speedups 5-10
  points below the instrumented truth (FINDINGS section 8).
- `stop` replies and `run_experiment` runs report `delay_counters`.
- `validate_demo.py` reports the real change on the plain program and under
  libcoz (idle); the exit code still uses the plain program.

## Requirements

- R1 Bank mode: `local_delay += needed`, `overshoot_bank += waited - needed`;
  pay from the bank before pausing; reset the bank at each experiment epoch.
- R2 Default `propagate` in all modes; bank only with
  `COZ_PAUSE_OVERSHOOT=bank`.
- R3 Counters: sample credit, pushed global delay, pauses, paused ns,
  overshoot ns, bank used, deferred payments, pauses in a lock, skips,
  skipped ns; reported as deltas in the `stop` reply.
- R4 `run_experiment` run entries carry `delay_counters`.
- R5 Diagnostic toggles used in the investigation (sample period, timer
  slack, per-section credit, spin pauses, cpu-clock) are not shipped; their
  patch is kept as `diagnostic-toggles.patch`.

## Acceptance Criteria

- AC1 With bank, 4-thread demo speedup (pthread and spin-hooked): global
  delay and pushed delay <= 1.2x sample credit; no pause inside a lock.
- AC2 Default behaviour unchanged (propagate); validation results recorded
  for both modes against both truths.
- AC3 ctest and `uv run pytest` pass.

## Testing Plan

- Integration test `test_speedup_global_delay_matches_samples_with_bank`
  (pthread, spin-hooked, sets `COZ_PAUSE_OVERSHOOT=bank`): fails with
  propagate (2.02 s global vs 0.37 s credit), passes with bank.
- `scripts/probe_speedup.py` for per-run diagnostics; `validate_demo.py`
  before (`--env COZ_PAUSE_OVERSHOOT=propagate`) and after.

## Implementation Plan

1. libcoz: overshoot mode + counters (profiler.h/.cpp, thread_state.h,
   libcoz.cpp env).
2. server: pass `delay_counters` through `_one_run`.
3. scripts: probe_speedup.py, probe_latch.py, validate_demo.py truth column.
4. docs: architecture accuracy list and limitation, README validation table.
5. Open: bank's ~5-6 point residual below the instrumented truth (S
   samples per transaction drop in some bank runs; mean residual
   unexplained); per-CPU sampling events (need `perf_event_paranoid <= 0` or
   CAP_PERFMON) to avoid the per-thread PMU cost; mysqld 0% experiment cost.
