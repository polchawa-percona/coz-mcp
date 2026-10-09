# Progress: AIO hand-off (PS-11508)

## Done
- coz `coz-mcp-aio-handoff` dac7103 (pushed): `_coz_get_latch_api2`
  (acquired_handoff, release_handoff, pre_block, post_block), registry
  hand-off counts, `held_now`, `lock_depth_threads/raised/max`; 5 new ctest
  cases in tests/latch (59/59 assertions pass).
- PS `PS-11508-8.4-coz-hooks` db4bcea7a27 (pushed): pass-aware hooks, release
  hook moved to unlock wrappers, buf_page_get_zip poll bracketed, os0file.cc
  io_getevents comment. Build OK (RelWithDebInfo, WITH_COZ=ON); debug and
  no-hooks variants syntax-checked; MTR 11 tests pass (--nowarnings: shell
  runs at nice 5, so InnoDB thread-priority warnings fail every test).
- coz-mcp `aio-handoff`: latch_top passes through hand-off / lock depth
  fields (+2 pytest).

## Builds
- `~/builds/8.4-coz` (shared, reused); baseline binaries in
  `~/builds/8.4-coz/before/` and `runtime_output_directory/mysqld.before`.
- `~/builds/coz-mcp-aio` (delete after push).

## Smoke
- Script: `~/builds/8.4-coz/aio-smoke/smoke.py before|after out.json`
  (mysqld CPUs 16-23, sysbench 24-31, 64M buffer pool).
- Results: see docs/mysql.md section on asynchronous I/O.

## Update
- Merged coz-mcp-control 5ffaad5 into coz-mcp-aio-handoff (23393f6); coz-mcp
  `aio-handoff` rebased onto main 3b37294. ctest 5/5, pytest 65/65 (fresh
  build `~/builds/coz-mcp-aio`).
- Smoke before/after/after-merged: before 1.58M unmatched + overflows after
  read_write; after 0 / 0, hand-off acquires == releases, held_now 0 and
  lock depth max 1 at idle (cond-wait threads). 0% experiment overhead about
  -9% before and -9..-10% after (noise, busy host). JSON in
  `~/builds/8.4-coz/aio-smoke/`.
- Open: buf_LRU_get_free_block legacy 10 ms sleep (~10k waits/min) not
  instrumented.
