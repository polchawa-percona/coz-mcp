# coz-mcp v1 (PS-11508)

Goal: learn MCP by building an MCP server that lets an agent drive Coz causal-profiling
experiments at runtime against a process under load (sysbench + mysqld, or a demo
program), including artificial slowdowns.

## As Is

- No `coz-mcp` project exists (`~/Projects/coz-mcp` is an empty repo).
- Upstream Coz (github.com/plasma-umass/coz, last commit 2026-07-10):
  - `libcoz.so` is LD_PRELOADed via `coz run --- <cmd>`. Configuration is read from
    environment only at startup (`COZ_FIXED_LINE`, `COZ_FIXED_SPEEDUP`, scopes, output).
  - `profiler::profiler_thread` (`libcoz/profiler.cpp:200`) runs an autonomous loop:
    pick a random sampled line (or fixed line), pick a random speedup in 5% steps
    (25% weight on 0%), run an adaptive-length experiment (>= 500 ms), cool off, repeat.
    Results are appended to `profile.coz`.
  - Virtual speedup: when a perf sample (1 ms period, processed in batches of 10) hits
    the selected line, the thread adds `delay_size` to its `local_delay`; other threads
    catch up by `nanosleep` in `add_delays` (`profiler.cpp:678`). `_global_delay` is
    the total virtual delay; it is subtracted from experiment duration.
  - Blocking calls interposed (`libcoz/libcoz.cpp`): pthread mutex/cond/rwlock/barrier/
    join, sem_*, signals. Not interposed: raw futex, epoll, recv/read.
  - Progress points: only `COZ_PROGRESS*` macros in source; breakpoint/sampling
    progress points are disabled (`FATAL "Sampling-based progress points are
    temporarily unsupported"`).
  - No runtime control, no slowdown mode.
- Environment: Python 3.12, uv, cmake 3.28, ninja, gcc 13, clang 15/18. GitHub user
  `polchawa-percona`. `kernel.perf_event_paranoid=4` (Coz needs <= 1), sysbench not
  installed, no passwordless sudo. PS build `~/builds/8.4-release` (RelWithDebInfo, gcc,
  no explicit frame pointers).
- MCP Python SDK is at v2 (`pip install mcp`): `from mcp.server import MCPServer`,
  `@mcp.tool()`, `@mcp.resource()`, `@mcp.prompt()`, `ctx.report_progress()`,
  in-memory testing via `mcp.Client(server)`; protocol revision 2026-07-28
  (server-to-client progress only; `ctx.info()` logging deprecated).

## To Be

Repository `~/Projects/coz-mcp` (GitHub: `polchawa-percona/coz-mcp`):

```
coz/        git submodule -> polchawa-percona/coz (fork), branch coz-mcp-control
server/     Python package coz_mcp (uv project), MCP server over stdio
demo/       C++ demo with a known bottleneck, prints sysbench-format report lines
scripts/    run-demo.sh, run-mysqld.sh (start target under coz with control socket)
docs/       architecture.md (MCP concepts explained), mysql.md (PS recipe)
```

Flow:

1. User starts target under patched Coz with `COZ_CONTROL_SOCKET=/path.sock`
   (manual mode: Coz runs no autonomous experiments).
2. MCP client (Claude Code) launches `coz-mcp` over stdio, configured with the socket
   path and workload (sysbench or demo) command.
3. Agent calls tools: start workload, list hot lines, run speedup/slowdown experiments
   on chosen lines while the workload runs, compare against interleaved baselines,
   read corrected TPS results.

## Requirements

### A. Coz fork (C++)

- R1 Control socket: when `COZ_CONTROL_SOCKET` is set, libcoz listens on that unix
  socket. The listen/accept/read loop runs inside `profiler_thread` itself (`poll` with a
  short timeout), which upstream already creates with `real::pthread_create`
  (`profiler.cpp:179`) and never samples, so no new profiled thread appears. Protocol:
  one JSON object per line, one response line per request; one client at a time.
- R2 Manual mode: when control is enabled, `profiler_thread` replaces the autonomous
  loop (`profiler.cpp:256-490`) with a command loop, and skips the wait for progress
  points (`profiler.cpp:239-249`) because mysqld has none. The loop exits when
  `_running` becomes false so shutdown join (`profiler.cpp:545`) does not hang. Without
  the env var, upstream behaviour is unchanged. Manual experiments are also appended to
  `profile.coz` in upstream format (speedup kind only) so `coz plot` keeps working.
- R3 Commands:
  - `status` -> `{mode, experiment_active, line, kind, percent, global_delay_ns,
    slowdown_ns, now_ns}`
  - `hot_lines {min_samples}` -> all in-scope lines with cumulative sample count >=
    min_samples `[{line, samples}]` (cumulative since start; the MCP server diffs
    snapshots to get recent hot lines).
  - `start {line: "file:line", kind: "speedup"|"slowdown", percent: 0..100,
    max_duration_s}` -> `{start_ns, global_delay_ns, warning?}`; error if line unknown or
    experiment active; `warning` if the line has 0 samples. Coz auto-stops the
    experiment after `max_duration_s` (default 120) so a dead client cannot leave the
    target slowed. Order: set selected line and delay size, then `_experiment_active`.
  - `stop` -> `{start_ns, end_ns, global_delay_ns_delta, slowdown_ns_delta,
    selected_samples, auto_stopped}`; time and `_global_delay` are read before
    `_experiment_active` is cleared. Error if no experiment was run since last `stop`.
  - unknown command / malformed JSON -> `{"error": "..."}`, connection stays usable.
- R4 Slowdown kind: `_delay_size` is 0 during a slowdown experiment, so a hit is never
  credited as speedup (`profiler.cpp:748`). In `process_samples`, after the sample loop,
  the thread sleeps `hits * percent% * SamplePeriod`. Slept time is added to a global
  `slowdown_ns` counter and NOT to `local_delay` / `_global_delay`, so no other thread
  pauses for it. Known limit: samples are processed every ~10 ms, so the sleep lands in
  the same critical section only when an interposed unlock (`catch_up`, `libcoz.cpp:451`)
  runs it first - true for pthread mutexes (demo), not for InnoDB's own mutexes/futexes.
- R5 Speedup kind uses upstream virtual-speedup accounting unchanged.
- R6 (moved to v1.1) Timer slack reduction.

### B. MCP server (Python, MCP SDK v2, stdio)

- R7 Configuration via env/CLI: control socket path, workload preset (`sysbench` or
  `demo`) with its binary and base args. Tools never execute arbitrary binaries.
- R8 Tools (long operations non-blocking or with progress notifications):
  - `coz_status()`
  - `hot_lines(limit=20, path_filter=None)`
  - `start_workload(extra_args=[], duration_s)` / `stop_workload()` /
    `workload_tps(last_s=10)` - workload runs in background, report lines parsed into a
    time series.
  - `run_experiment(line, kind, percent, duration_s=20, repeats=3, warmup_s=3)` -
    alternates 0% baseline and target runs on the same line (`repeats` pairs), reports
    progress notifications, returns per-run and mean/stddev raw and corrected TPS, and
    delta vs baseline. Only one experiment at a time: concurrent calls are rejected.
  - (moved to v1.1) `sweep` - the agent composes it from `run_experiment`.
- R8a Robustness: workload is killed when the MCP server exits; control client
  reconnects when the target restarts; a clear error explains `perf_event_paranoid`
  when the socket is missing because Coz failed to start sampling.
- R9 Correction is per report interval: when each workload report line arrives, the
  MCP server samples `global_delay_ns` via `status`. For interval i:
  `corrected_tps_i = txns_i / max(len_i - delta_delay_i, 0.1 * len_i)` (clamped as
  upstream does). Experiment TPS = sum(txns_i) / sum(corrected len_i) over intervals
  fully inside the experiment window. For `slowdown` and 0% baselines no correction is
  applied. Known bias: the sysbench client runs outside virtual time; accepted because
  client and server are pinned to disjoint CPU sets (documented assumption).
- R9a `start_workload` rejects `--rate` (open-loop load makes corrected TPS invalid).
- R10 Resources: `coz://experiments` (JSON log of all results this session),
  `coz://hot-lines` (latest hot lines).
- R11 Prompt: `find_bottleneck` - instructions for an agent to rank hot lines by
  speedup/slowdown sensitivity.

### C. Demo and validation

- R12 `demo/`: N threads; each transaction does parallel work (line P) then serial work
  inside a lock (line S). Prints `[ Ns ] ... tps: X ...` lines every second (sysbench
  format). Flag `--serial-work-pct` scales serial work (ground truth for real
  speedup/slowdown). Flag `--lock=pthread|spin|spin-hooked`: `spin` is an inline
  test-and-test-and-set lock with a pthread_cond fallback (same shape as InnoDB
  `TTASEventMutex`), `spin-hooked` adds the latch hooks from R16.
- R13 Validation script: slowdown of S lowers TPS clearly; slowdown of P (with enough
  threads that S saturates) changes TPS much less (pass/fail). Speedup prediction for S
  at 20% vs real `--serial-work-pct=80` is reported side by side (no pass/fail in v1).

### E. Slowdown inside InnoDB mutexes and rw-latches

Problem: InnoDB latches (`PolicyMutex<TTASEventMutex>` by default, `MUTEXTYPE=event`,
and `rw_lock_t`) are inline code, so LD_PRELOAD cannot interpose them. Coz only runs
`catch_up` from the interposed `pthread_mutex_unlock` (`libcoz.cpp:451`); for InnoDB
latches the first interposed call happens inside `os_event_set` after the latch word
was already released (`ib0mutex.h:245-261`, `sync0rw.ic:377-412`), and only when there
are waiters. So a slowdown hit in a critical section is paid after release (or ~10 ms
later), which does not model "this code under the latch is slower". Waiting side is
fine: waiters block in `os_event` -> `pthread_cond_wait`, which Coz interposes.

- R16 Latch hook API: `coz.h` in the fork gets `COZ_LATCH_TRANSITION()`, resolved once
  via `dlsym` like `COZ_PROGRESS` (null pointer -> no-op when not run under coz). It calls
  upstream's exported `coz_catch_up()`: drain this thread's pending perf samples, pay
  slowdown debt and speedup delays now.
- R17 Rule "drain on every latch transition": the hook runs right before acquiring and
  right before releasing a latch. Effect: samples drained at release were taken inside
  the critical section, so their slowdown is paid while the latch is still held; samples
  taken outside any latch are paid before the next acquire, i.e. outside. For speedup
  experiments it publishes the holder's delay credit before waiters are woken (same as
  Coz does for pthread mutexes).
- R18 Percona Server patch (gated by CMake `-DWITH_COZ=ON`, no code change otherwise):
  hook in `PolicyMutex::enter/exit` (`ib0mutex.h`, covers every `ib_mutex_t` type) and in
  rw-lock S/X/SX lock and unlock functions (`sync0rw.ic`, `sync0rw.cc`), including
  `Sharded_rw_lock` users (lock_sys) which use `rw_lock_t`. Server-layer locks use
  pthread primitives and are already interposed.
- R19 Overhead: hook cost with coz not loaded and with coz loaded but idle is measured
  on the demo (`spin` vs `spin-hooked`) and reported; baseline (0%) experiments run with
  the same hooks, so overhead cancels in deltas.
- Not covered (documented): atomics-based waits and spin loops not built on these
  primitives (e.g. parts of redo log writer/flusher coordination).

### F. Latch wait/hold statistics per acquire site

Goal: real-time ranking of InnoDB mutex / rw-latch acquire sites (`file:line` of the
caller, plus latch name) by time spent waiting to acquire and time spent holding.
InnoDB already passes the caller location to every acquire (`mutex_enter` ->
`UT_LOCATION_HERE`, `rw_lock_*_lock(L, UT_LOCATION_HERE)`), and the latch id is known
(`latch_id_t` in the mutex policy and in `rw_lock_t`). PFS has no hold time and no
per-source-line aggregation, so this is collected by our own hooks.

- R20 Hook API extended (fork `coz.h`, all no-ops without libcoz):
  `COZ_LATCH_WAIT_BEGIN(latch, file, line, name, mode)` before acquire,
  `COZ_LATCH_ACQUIRED(latch)` after acquire, `COZ_LATCH_RELEASE(latch)` before release.
  These replace the plain `COZ_LATCH_TRANSITION` (they also do the R17 drain).
  `mode` = mutex | s | x | sx.
- R21 libcoz aggregates per key (acquire file:line, latch name, mode): count, wait sum,
  wait max, hold sum, hold max, log2 histograms for wait and hold (percentiles).
  Thread-local held-latch stack records (latch, key, acquire time); release matches the
  latest entry for the latch. Counters are per-thread (no shared cache lines on the hot
  path) and summed when queried. Release by another thread (rw-lock `pass` != 0) or an
  unmatched release: hold time not recorded, counted in `unmatched_releases`.
  Clock: `CLOCK_MONOTONIC` via vDSO (or calibrated TSC if cost matters, decided after
  measuring).
- R22 Off by default; control commands `latch_stats {enable: bool}`, `latch_stats_reset`,
  `latch_stats_snapshot` -> all keys with cumulative counters + `now_ns`. When disabled the
  hooks only do the R17 drain.
- R23 MCP tool `latch_top(metric="wait"|"hold", window_s=5, limit=20, latch=None,
  path_filter=None)`: two snapshots `window_s` apart, diff, rank by total time in window;
  returns per key: site, latch, mode, count/s, total ms/s, avg, p50/p99 (from
  histogram), max. Also `latch_stats_enable(on)` and `latch_stats_reset()`.
- R24 MCP resource `coz://latches/top` (latest window ranking, `wait` and `hold`),
  refreshed by a background poller while stats are enabled; clients that subscribe get
  `resources/updated` notifications (exercise MCP subscriptions).
- R25 Caveats reported in tool output: during a Coz experiment, times include injected
  delays (wanted for slowdown, not corrected for speedup); hold time of a site covers the
  whole critical section, not the site line itself, so `run_experiment` targets come
  from `hot_lines` inside that section.

### G. Separate track: Performance Schema aggregation per source line (PS branch)

Done by a separate agent on its own PS branch (`PS-11508-8.4-pfs-latch-by-source`), with
its own REQUIREMENTS.md in the PS repo. Not a dependency of coz-mcp v1.

As is (PS 8.4): PFS has the acquire source at wait start (`src_file`/`src_line` passed
to `start_mutex_wait` / `start_rwlock_*wait`, `pfs.cc:724-840`) but aggregates waits
only per instance / class / thread / account; per-source data exists only in the sampled
`events_waits_history*` tables. Hold-time accounting exists but is compiled out
(`#ifdef LATER_WL2333`, `pfs.cc:4881-4896`, `4958-4972`) and is per instance, with no
per-reader tracking for rwlocks.

- G1 New table `performance_schema.events_waits_summary_by_source` (name to be agreed):
  key `EVENT_NAME, SOURCE (file:line), OPERATION` for mutex and rwlock instruments
  (InnoDB latches via PFS keys and server `mysql_mutex`/`mysql_rwlock`/`mysql_prlock`),
  columns `COUNT_STAR, SUM/MIN/AVG/MAX_TIMER_WAIT`, and hold columns
  `COUNT_HOLD, SUM/MIN/AVG/MAX_TIMER_HOLD`.
- G2 Hold time per acquire source: bounded per-thread stack of held instrumented locks in
  `PFS_thread` (acquire source + start time); unlock pops the matching entry; overflow and
  unmatched unlocks counted, not recorded. Works for concurrent S holders.
- G3 Storage: fixed-size lock-free hash (like the statement digest table), sized by new
  read-only sysvar `performance_schema_max_latch_source_summaries` (-1 = autosize, 0 =
  disabled); status `Performance_schema_latch_source_summaries_lost`.
- G4 Runtime control: new `setup_consumers` row (e.g. `latch_source_summary`), off by
  default; only timed instruments contribute.
- G5 `TRUNCATE TABLE` resets the table.
- G6 Tests: MTR for table content (debug_sync to force a known wait and hold), consumer
  on/off, truncate, sysvar basic test, runs with sysvar values 0 / small (lost counter
  grows) / default; re-record `sys_vars`/`perfschema` results listing variables and
  tables.
- G7 Overhead measured with sysbench oltp_read_write: consumer off vs on.
- G8 coz-mcp follow-up (v1.1): tool `pfs_latch_top` reading the table over a MySQL
  connection, for cross-checking R23.
- Risk: a new Percona-only PFS table and sysvar touch many upstream files
  (`storage/perfschema`, `sql/sys_vars.cc`, result files), which raises merge cost.

### D. Packaging

- R14 `README.md`: build steps, `.mcp.json` example for Claude Code, required sysctl.
- R15 (moved to v1.1) `docs/mysql.md`: build PS with `-g -fno-omit-frame-pointer`, run
  mysqld under coz with `COZ_SOURCE_SCOPE`, sysbench recipe, InnoDB mutex limitation.

## Acceptance Criteria

- R1: a test client connects, sends `{"cmd":"status"}`, receives valid JSON; target
  without env var opens no socket.
- R2: demo under coz with control set and no commands -> `profile.coz` has no experiment
  records after 5 s; without env var upstream experiments appear.
- R3: each command returns documented fields; malformed input returns `error` and next
  request on same connection succeeds; `start` on unknown line errors.
- R4: unit test of accounting (`_delay_size` stays 0): slowdown hit increases `slowdown_ns`, leaves
  `local_delay`/`_global_delay` unchanged; integration: slowing S by 50% drops demo
  TPS >= 15% (estimate).
- R5: speedup run on demo produces `global_delay_ns_delta > 0`.
- R7: tool call with workload args cannot change the binary; unknown preset rejected.
- R8: each tool callable via in-memory `Client(server)` with fake control socket and fake
  workload; `run_experiment` emits progress notifications.
- R9: unit tests with synthetic report series + delay samples give exact expected
  values, including the clamp and partial intervals excluded.
- R8a: MCP server exit kills fake workload; restart of fake control server is survived.
- R10/R11: resources/prompt listed and readable through in-memory client.
- R12: demo builds, prints parseable lines, `--serial-work-pct` changes TPS.
- R13: validation script prints table and pass/fail per check.
- R16/R17: demo `spin-hooked`, slowdown of S by 50%: TPS drop close to the `pthread`
  lock result (within 1/3 relative, estimate), while `spin` (no hooks) shows a clearly
  smaller drop. Unit test: hook resolves to no-op without libcoz.
- R18: PS builds with and without `WITH_COZ`; without it, the preprocessed latch code is
  unchanged; mysqld under coz with sysbench oltp_read_write shows `hot_lines` and a
  slowdown of a line inside `lock_sys` or `trx_sys` mutex section lowers TPS.
- R19: README table with hook overhead per latch operation (ns).
- R20/R21: demo `spin-hooked` with known serial work: `hold` avg of S site within 20% of
  the work time measured without coz (estimate); `wait` of S grows with thread count;
  unit tests for the held-latch stack (nested, out-of-order release, unmatched release)
  and histogram percentiles.
- R22: disabled hooks record nothing; reset zeroes counters; snapshot JSON schema test.
- R23: in-memory MCP test with a fake control server returning two snapshots -> exact
  ranking and rates.
- R24: resource listed; subscribed client receives an update notification.
- R25: caveat text present in `latch_top` output when an experiment is active.
- R19 extended: overhead per latch operation with stats disabled and enabled.
- R14/R15: following README on a fresh clone builds everything (manual check).

## Testing Plan

- C++ (Coz fork): ctest unit tests for protocol parsing/dispatch (pure function, no
  socket) and slowdown accounting (extracted helper); integration test script running
  demo under coz with control socket (requires perf_event_paranoid <= 1; skipped with a
  message otherwise).
- Python: pytest
  - parser for sysbench/demo report lines (real sysbench 1.0 output samples),
  - correction math (R9),
  - control client against an in-process fake unix-socket server,
  - workload runner against a fake script that prints report lines,
  - MCP tools/resources/prompt through in-memory `Client(server)`.
- End-to-end: `scripts/validate-demo.sh` (R13).
- Manual: Claude Code with `.mcp.json`, demo then mysqld + sysbench.

## Implementation Plan

1. Repo skeleton: README stub, .gitignore, coz submodule (fork + branch). Test: submodule
   builds unchanged (`cmake && ninja`), upstream ctest passes.
2. Demo program + report format. Test: build, run 3 s, output parsed by a small Python
   test fixture.
3. Python: report parser + correction math (TDD, pure functions).
4. Python: workload runner (background subprocess, time series). Test: fake script.
5. Coz: JSON-line protocol dispatcher as pure function + ctest.
6. Coz: control thread + socket (env gated). Test: integration with demo, `status`.
7. Coz: manual mode in `profiler_thread`; `start`/`stop`. Test: integration, no
   autonomous records; start/stop returns fields.
8. Coz: slowdown kind + counters + auto-stop. Test: accounting unit test, demo TPS
   drop.
9. Coz: `hot_lines`. Test: integration returns demo lines S/P.
10. Python: control client. Test: fake socket server.
11. Python: MCP server tools one by one (status, hot_lines, workload, run_experiment),
    then resources and prompt. Test: in-memory Client per tool.
12. Validation script on demo (R13).
12a. `COZ_LATCH_TRANSITION` in fork `coz.h`; demo `spin`/`spin-hooked`; validation
     extended with R16/R17 checks and R19 overhead.
12c. libcoz latch stats (R20-R22) with unit tests; demo hooked through the new API.
12d. MCP `latch_top`, enable/reset tools, `coz://latches/top` resource + subscription.
12b. PS 8.4 patch with `WITH_COZ` hooks (R18, R20) on branch `PS-11508-8.4-coz-hooks`, build in `~/builds/8.4-coz`
     (RelWithDebInfo, `-fno-omit-frame-pointer`), sysbench smoke run.
13. Docs: README, architecture.md (MCP concepts walkthrough).
14. (v1.1) mysql.md and manual run against PS 8.4 + sysbench (needs sysbench, PS
    rebuilt with frame pointers in `~/builds/8.4-coz`).

## Open points / assumptions

- Commit title format adapted: `PS-11508 [coz-mcp] <title>` (no server branch applies).
- Lad MCP review tool is not available in this environment; design/code reviews use a
  review subagent and the advisor instead.
- User action needed before integration steps (6+): `sudo sysctl
  kernel.perf_event_paranoid=1` and `sudo apt install sysbench`.

## Deviations found during implementation (2026-10-09)

- R5 (speedup accounting unchanged) was not kept. Validation against real code
  changes on the demo showed upstream semantics give wrong answers for
  lock-bound code (slowdown of the critical section -9% vs real -30%, slowdown
  of parallel code -65% vs -4%, speedup +30% predicted -4% vs real +42%).
  Changes in the fork, on by default in control mode and switchable back to
  upstream behaviour (`COZ_LOCK_AWARE_DELAYS=0`, `COZ_SAMPLE_EVENT=task-clock`):
  - virtual delays are deferred while a thread holds a lock (pthread or
    COZ_LATCH_*), bounded by 20 ms; samples are drained before each acquire;
    latch waits count as blocked; pre_block/post_block nest;
  - slowdown debt is paid by spinning, in proportion to each critical section
    (or to the code since the last release), instead of 0.5 ms sleeps per
    sample;
  - sampling uses ref-cycles (period calibrated to 1 ms of CPU time) because
    the software task clock under-sampled code just before a lock handoff
    (5% instead of 26% of the samples on the demo).
  After the fixes all checked validation cases agree with the real change
  within max(5 points, 1/3 of the real change).
- R2: manual experiments are written to the profile as `manual-experiment`
  records (both kinds) rather than upstream `experiment` records, because
  without progress points upstream records carry no throughput.
- R13 became a paired comparison with ground truth measured in the same
  session (`scripts/validate_demo.py`) instead of fixed thresholds.
- R19 measured on the demo (single thread, estimates): ~0 ns per lock/unlock
  pair without libcoz, ~7 ns with libcoz idle, ~75 ns with latch stats on.
- Coz exports `_coz_add_delays` (not `coz_catch_up`, which is macOS-only); the
  latch hooks call into libcoz through the new `_coz_get_latch_api` table.
