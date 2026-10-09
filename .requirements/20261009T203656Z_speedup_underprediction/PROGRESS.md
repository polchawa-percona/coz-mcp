# PROGRESS - speedup under-prediction (PS-11508)

- Worktree: ~/Projects/coz-mcp/.claude/worktrees/speedup-underprediction (branch `speedup-underprediction`)
- Coz submodule branch: `coz-mcp-speedup-underprediction` (from `coz-mcp-control` 05efea7)
- Build: ~/builds/coz-mcp-underprediction (RelWithDebInfo, Ninja); delete after push
- Host: shared with another agent's MTR run (mysqld-debug, unpinned); load 11-30. Numbers noisy.

## Hypotheses
- H1 deferred payment vs hand-off ordering
- H2 MaxDeferredDelay cap (deprioritized: delay per sample 0.3 ms << 20 ms)
- H3 sampling attribution / ref-cycles period calibration
- H4 correction math / interval alignment
- H5 real change non-linear
- H6 lump size: 0.3 ms delay per sample paid in lumps (advisor)
- H7 hook overhead h inside critical section during any experiment (coordinator): gain (S+h)/(0.7S+h)

## Steps
- [ ] 1. probe script: dump runs[] (raw/corrected tps, global delay, samples) for 1/2/4 threads, pthread + spin-hooked
- [ ] 2. real ground truth at same thread counts; S share at 1 thread
- [ ] 3. H7: TPS no experiment vs 0% experiment (1 and 4 threads)
- [ ] 4. H3: total samples/s per busy thread
- [ ] 5. instrument counters / env toggles as needed
- [ ] 6. fix + tests + validate_demo before/after
- [ ] 7. commit, push branches, delete build dir

## Log (UTC 2026-10-09)
- 20:37 probe script scripts/probe_speedup.py; 1-thread: coz +11.2% vs real +8.4% (S share 28.5% by samples and by removing S); H3/H4 refuted at 1 thread.
- 4 threads (upstream propagate): global delay 5.7x sample credit (overshoot feedback), raw TPS halves, x=0.65; pthread coz 37.7/42.4/39.3 vs real 40.1/40.8; spin-hooked 32.4/33.4 vs 44.1.
- counters added (stop reply delay_counters), env toggles COZ_SAMPLE_PERIOD_NS, COZ_TIMER_SLACK_NS, COZ_PAUSE_OVERSHOOT=propagate|drop|bank.
- bank: global == sample credit, raw ratio 0.98, coz +28.0 (pthread) / +26.5 (hooked): stable but lower. paused_in_lock always 0 (H2 refuted).
- period 100us (H6) does not help (bank_p100 +23.1, bank_p100_s1 +21.2); slack 1ns + bank +22.9.
- Key: corrected <= base/(1-0.3*u_S), u_S = S share of wall (0.77-0.8 by samples) -> max ~+30%; real +41% needs non-S serial gap to shrink (base gap ~1.2us -> variant ~0.9us). Next: measure contention (latch stats) idle vs variant vs experiment.
- CORRECTION: "max ~+30%" bound above is wrong (ideal raw should rise to ~1.04, x ~0.26) - advisor.
- section credit (hold/spread) + spin pauses: no improvement (+20..25%), raw stays 0.95-0.96 -> H6 lumps refuted.
- 23:10 host quieter (load 4-8). latch probe spin-hooked: libcoz idle 130k, variant (serial 70%) under libcoz idle 162k (+25%); bank exp +23%.
- KEY: real change measured under libcoz idle: pthread plain +40.2% vs libcoz idle +28.8%; spin-hooked plain +44.6% vs +29.3%.
  Variant under libcoz: ref-cycles 160-166k, cycles 162k, ref-cycles with 1s period (no samples) 160k, task-clock 183k, plain 186k.
  `perf stat -e ref-cycles` alone (no libcoz): variant 169k -> 140k (-15%), base -3%; ctx switches 187k -> 518k per 4s.
  => observer effect of per-thread HW perf events on this AMD Zen5 host (paranoid=1, per-CPU events not allowed).
- task-clock / cpu-clock: cheap but S gets 6-12% of samples instead of ~27% -> predictions +10%.
- 23:3x paired quiet-host run (load 4-5): pthread propagate +32.2 / bank +27.9 vs real under libcoz +30.2/+29.8, plain +41.9/+40.6; spin-hooked propagate +29.7 / bank +25.8 vs under libcoz +29.6/+30.1, plain +46.0/+44.8.
- Decision: ship bank (default in manual mode) + delay_counters; drop other toggles (saved diagnostic-toggles.patch). Integration test added (fails with propagate: 2.02 s vs 0.37 s credit).
- ctest 5/5, pytest 63 passed. validate_demo before/after running in background -> /tmp/validate_before_after.txt
- 2026-10-10 00:1x validate_demo (quiet, load 3-5): before(propagate) speedup pthread +37.7 vs libcoz +31.1 / plain +40.6; spin-hooked +32.7 vs +31.0 / +45.1.
  after(bank) speedup pthread +18.2 vs +30.5 / +41.7 (FAIL); spin-hooked +25.7 vs +30.7 / +45.5. Slowdown P under libcoz -0.6..-1.0 vs plain -4.4..-4.9 (coz -6.4..-7.7): FAIL vs libcoz truth both modes.
- bank pthread probes (quiet): +19.5, +19.4, +26.4. x 0.19-0.26. S samples per txn in 30% runs drop up to 16% vs 0% runs in some runs (5.13 vs 6.13 per ktxn), P samples per txn rise; samples per user-CPU-s constant (988). Not seen in propagate runs. Unexplained.
- Decision (advisor): bank opt-in, default propagate; validate exit code on plain column; libcoz column informational. Rebuilt; ctest 5/5, pytest 63 passed; default verified propagate (2.0 s global vs 0.34 s credit).
- Next: README table, commit coz submodule + main repo, push, delete build dir.
