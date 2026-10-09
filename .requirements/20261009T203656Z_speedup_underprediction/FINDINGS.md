# FINDINGS - virtual speedup under-prediction (PS-11508)

All runs: demo pinned to CPUs 8-13 (AMD Ryzen 9 9955HX, Zen5, 16C/32T, kernel 7.0,
`perf_event_paranoid=1`), speedup of `demo.cc:58` (COZ-LINE S) by 30% unless noted,
`scripts/probe_speedup.py` (per-run details of `run_experiment`, 5 s windows,
1 s warmup). The host was shared with another agent's MTR run: load average
15-30 until ~23:05 UTC, 4-8 afterwards, 16 again from ~23:20. Numbers from
different load phases are not comparable; within one probe invocation arms are
interleaved. Treat single numbers as +-3 points (estimate from repeats).

Notation: `x` = global delay / run wall time, `raw ratio` = raw TPS of the 30%
runs / raw TPS of the 0% runs, `credit` = delay credited for selected-line
samples (`delay_counters.sample_credit_ns`), `pushed` = growth of the global
delay counter.

## 1. One thread (no contention) - H3/H4

| | value |
|---|---|
| S share from removing S (`--serial-work-pct=0`) | 28.4% |
| S share from samples (hot_lines, 993 samples/s per thread) | 28.5% |
| real change (serial 70%) | +8.4% (expected 1/(1-0.3*0.284) = +9.3%) |
| coz-mcp (upstream accounting) | +11.2% [8.8, 13.7] |
| global delay vs credit | 497 ms vs 495 ms |

Sampling rate and attribution (ref-cycles calibration, `profiler.cpp`
set_sample_event) and the correction (`metrics.py` window_tps) are right at 1
thread: H3 and H4 refuted. The 30% runs had a raw TPS 2.3% above baseline
(open, small).

## 2. Four threads, upstream accounting (overshoot propagated)

| case | load | coz-mcp | real (plain) | x | raw ratio | pushed / credit |
|---|---|---|---|---|---|---|
| pthread | 16 | +37.7% | +40.1% | 0.655 | 0.477 | - |
| pthread (repeat) | 19 | +42.4% | +40.8% | 0.657 | 0.490 | - |
| pthread (repeat) | 18 | +39.3% | - | 0.656 | 0.480 | 5.7 |
| spin-hooked | 17 | +32.4% | +44.1% | 0.648 | 0.469 | - |
| spin-hooked (repeat) | 18 | +33.4% | - | 0.651 | 0.465 | 5.5 |
| pthread, 2 threads | 19 | +7.1% | +10.7% | 0.47 | 0.567 | - |
| spin-hooked, 2 threads | 19 | +6.9% | +8.6% | 0.42 | 0.625 | - |
| pthread | 4-5 | +35.4% [27.6, 43.1] | +41.1% | 0.66 | 0.463 | - |

Counters of one 4-thread pthread run (6 s): credit 687 ms, pushed 3929 ms,
181,719 pauses, 14.7 s paused in total of which 9.3 s overshoot, 0 pauses
inside a lock, 360 ms skipped by woken waiters.

Defect: `add_delays` (`profiler.cpp`, Linux branch) credits `waited` (the
nanosleep time including ~50 us timer-slack overshoot) to `local_delay`. The
paying thread is then ahead and pushes its overshoot into the global delay, so
every other thread pauses for it too, overshoots again, and so on. The global
delay grows ~5.7x faster than the samples justify, the program runs at half
speed during the experiment, and the corrected TPS depends on host sleep
latency rather than on the samples. This is consistent with the README's
run-to-run spread (+29.9% then, +35..42% now for the same case); there is no
paired instrumented truth from the high-load runs. The upstream macOS
branch already credits `needed` for the same reason.

## 3. Accounting variants (4 threads, pthread unless noted, load 15-20)

| variant (env) | coz-mcp | raw ratio | x | notes |
|---|---|---|---|---|
| propagate (upstream) | +39.3% | 0.48 | 0.656 | pushed 5.7x credit |
| drop (`COZ_PAUSE_OVERSHOOT=drop`) | +24.3% | 0.957 | 0.23 | overshoot 367 ms lost |
| bank (`COZ_PAUSE_OVERSHOOT=bank`) | +28.0% | 0.983 | 0.23 | pushed = credit |
| bank, spin-hooked | +26.5% | 0.981 | 0.226 | pushed = credit |
| timer slack 1 ns, propagate | +35.5% | 0.742 | 0.45 | 520k pauses |
| period 100 us, propagate | +39.1% | 0.496 | 0.64 | |
| period 100 us, drop | +3.1% | 0.82 | 0.20 | overshoot 3.75 s lost |
| period 100 us, slack 1 ns, propagate | +26.6% | 0.726 | 0.43 | |
| bank + slack 1 ns | +22.9% | 0.945 | 0.23 | |
| bank + period 100 us | +23.1% | 0.942 | 0.236 | |
| bank + period 100 us + slack 1 ns | +21.2% | 0.935 | 0.23 | |
| bank + credit p*hold per critical section (oracle) | +25.0% | 0.962 | 0.23 | |
| bank + sample-capped per-section credit | +23.4% | 0.954 | 0.228 | |
| bank + spread credit + spin pauses < 100 us | +19.8% | 0.947 | 0.211 | 740k spins |
| bank + hold credit + spin pauses < 100 us | +22.2% | 0.962 | 0.213 | |

Smaller or smoother delays (shorter period, per-section credit, spinning
instead of sleeping) do not move the prediction up: H6 (lump size) refuted.
No pause was ever paid while holding a lock (`paused_in_lock` = 0 in every
run): H1/H2 not supported. These toggles were diagnostic and are not part of
the change.

## 4. Ground truth under instrumentation (observer effect)

TPS of the demo, 4 threads, 6 s, load 4-8 (two runs each where shown):

| program | serial 100% | serial 70% | gain |
|---|---|---|---|
| plain, pthread | 133.7k / 135.2k | 188.5k / 187.6k | +40.2% |
| libcoz idle (ref-cycles), pthread | 130.1k / 128.4k | 166.4k / 166.9k | +28.8% |
| plain, spin-hooked | 130.4k / 130.6k | 188.3k / 189.1k | +44.6% |
| libcoz idle (ref-cycles), spin-hooked | 127.3k / 127.3k | 165.5k / 163.7k | +29.3% |

Variant (serial 70%, pthread) under libcoz idle with other sample events:

| sample event | TPS |
|---|---|
| ref-cycles, 1 s period (event open, ~no samples) | 160.8k / 160.4k |
| cycles | 162.4k |
| task-clock | 182.7k / 183.5k |
| plain (no libcoz) | 186.4k |

Same effect without libcoz, counting only (`perf stat -e EV demo ...`), load ~16:

| event | serial 100% | serial 70% | gain |
|---|---|---|---|
| none (plain) | 118.9k | 169.1k | +42.2% |
| task-clock | 118.6k | 168.7k | +42.3% |
| ref-cycles | 115.1k | 139.6k | +21.2% |
| cycles | 114.8k | 144.1k | +25.5% |
| ref-cycles:u / instructions:u (70% only) | - | 147.3k / 144.1k | - |

Context switches in 4 s of the variant: 187k with task-clock, 518k with
ref-cycles (2.8x). Per-CPU events (`-a -C 8-13`) are refused with
`perf_event_paranoid=1`.

Conclusion: a per-thread hardware perf event (ref-cycles, cycles,
instructions) slows the faster variant by 12-17% but the unchanged program by
only ~3% on this host, so the program Coz experiments on gains ~29% from the
real change, not ~41%. Mechanism (hypothesis, not measured directly): PMU
save/restore on context switches lengthens lock hand-offs once the lock is
not saturated, which makes more waiters sleep (2.8x more switches).

## 5. Software clocks attribute too few samples to S

| event | S share of samples (4 threads) | coz-mcp (bank) |
|---|---|---|
| ref-cycles | 27% | +21..28% |
| task-clock | 6% | +10.5% |
| cpu-clock | 12% | +10.4% |

Confirms the earlier finding that made ref-cycles the default (H3 holds for
software clocks only).

## 6. Lock behaviour from latch statistics (spin-hooked, load ~4)

| arm | raw TPS | corrected | hold/acquire | wait/acquire | lock busy |
|---|---|---|---|---|---|
| libcoz idle | 129.9k | - | 6.18 us | 7.54 us | 0.804 |
| 0% experiment | 130.7k | - | 6.12 us | 7.59 us | 0.801 |
| 30% speedup, propagate | 59.4k | 171.2k (+31%) | 6.32 us | 3.79 us | 0.372 |
| 30% speedup, bank | 121.4k | 161.1k (+24%) | 6.16 us | 6.26 us | 0.748 |
| real variant (serial 70%), libcoz idle | 162.3k | - | 4.34 us | 2.99 us | 0.706 |

In this single probe the bank run came close to the instrumented variant
(161.1k vs 162.3k), but the variant was measured in a different process and
S samples per transaction vary 6.1-6.6 per 1000 between processes, so this
is within noise; the paired runs of section 8 show bank below the
instrumented truth. Ideal real-time lock busy for a virtual speedup of that
variant: (0.706/0.7)/(0.706/0.7+0.294) = 0.77, measured 0.748.

## 7. H7 (overhead of an active experiment inside critical sections)

0% experiment vs no experiment (raw TPS ratio `base0/idle`): 0.99-1.02 in all
4-thread runs; latch hold time 6.12 us vs 6.18 us. Experiment-active overhead
is not measurable on the demo. The libcoz cost that does show is the
variant-specific HW-event effect of section 4, which a uniform h in
(S+h)/(0.7S+h) cannot produce. The ~9% drop seen on mysqld is not explained
by these measurements.

## 8. Paired runs on a quiet host (load 3-5), propagate vs bank

`probe_speedup.py`, 3 repeats, real change measured on the plain program and
on the program with libcoz loaded but idle, in the same invocation:

| lock | mode | coz-mcp | real under libcoz | real plain |
|---|---|---|---|---|
| pthread | propagate | +32.2% [26.0, 38.4] | +30.2% | +41.9% |
| pthread | bank | +27.9% [15.5, 40.3] | +29.8% | +40.6% |
| spin-hooked | propagate | +29.7% [28.1, 31.2] | +29.6% | +46.0% |
| spin-hooked | bank | +25.8% [23.9, 27.6] | +30.1% | +44.8% |

More bank runs, pthread (no truth arm): +19.5%, +19.4%, +26.4%.

`validate_demo.py` (6 s, 3 repeats), before = `COZ_PAUSE_OVERSHOOT=propagate`,
after = bank:

| case | propagate | bank | real under libcoz | real plain |
|---|---|---|---|---|
| slowdown S, pthread | -39.8% | -39.7% | -31.4% / -29.4% | -34.1% / -30.9% |
| slowdown P, pthread | -6.4% | -7.7% | -1.0% / -0.6% | -4.9% / -4.4% |
| speedup S, pthread | +37.7% | +18.2% | +31.1% / +30.5% | +40.6% / +41.7% |
| slowdown S, spin-hooked | -26.4% | -31.5% | -29.1% / -32.3% | -29.4% / -29.7% |
| speedup S, spin-hooked | +32.7% | +25.7% | +31.0% / +30.7% | +45.1% / +45.5% |
| slowdown S, spin (no hooks) | -7.6% | -8.7% | -32.2% / -32.3% | -29.7% / -29.6% |

(truth columns: value measured in the propagate / bank invocation).

S samples per transaction, target runs / 0% runs (from the probe JSONs):
bank 0.84, 0.85, 0.92, 0.97, 0.99, 1.02, 1.07, 1.12; propagate 0.96-1.05.
Rescaling bank's delay fraction by that ratio gives +23..28% (mean ~+24.6%)
for runs whose raw prediction ranged +18..28%: the per-run variation of S
samples explains the spread of bank results, not the ~5-6 point mean
residual below the instrumented truth (~30%). CPU per transaction stayed at
22.5 us and samples per user CPU second at 988 while S fell and P rose by
~0.8 us per transaction in a low run: an attribution shift is a hypothesis,
not measured.

Decision: bank stays opt-in (`COZ_PAUSE_OVERSHOOT=bank`); default remains
upstream `propagate`. Propagate's closeness to the instrumented truth on a
quiet host is compensation that depends on sleep overshoot (x = 0.65 from a
credit worth 0.12), not accuracy.

## 9. Slowdown of P under libcoz (separate open item)

The real 50% slowdown of the parallel line costs -4.4..-4.9% on the plain
program but only -0.6..-1.0% under libcoz (idle); coz-mcp reports
-6.4..-7.7%. Same observer-effect family as section 4 (the instrumented
program is more lock-bound), not investigated further.
