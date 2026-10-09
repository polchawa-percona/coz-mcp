# InnoDB asynchronous I/O hand-offs under coz-mcp (PS-11508)

## As Is

- InnoDB hands block latches to the I/O subsystem with a non-zero pass value:
  - page read: `rw_lock_x_lock_gen(&block->lock, BUF_IO_READ)` in
    `buf_page_init_for_read()` (buf0buf.cc:5261), released by
    `buf_page_io_complete()` (buf0buf.cc:6433) or `buf_read_page_handle_error()`
    (buf0buf.cc:5732), usually on an I/O handler thread;
  - page write: `rw_lock_sx_lock_nowait/_gen(..., BUF_IO_WRITE)` in
    `buf_flush_page()` (buf0flu.cc:1320/1362/1369), released by
    `buf_page_io_complete()` (buf0buf.cc:6451) or
    `buf_page_free_stale_during_write()` (buf0buf.cc:5906);
  - parallel read: `rw_lock_s_lock_gen(index lock, true)` (row0pread.cc:136/144).
- The coz-mcp latch hooks treat these like any latch: `acquired` raises the
  acquiring thread's `coz_lock_depth` and pushes a held entry in its latch
  stats; the release on the I/O thread lowers the I/O thread's depth (if > 0)
  and counts an unmatched release. The acquiring thread's depth stays raised
  for good, so its virtual delays are deferred and its slowdown debt is
  classified as "inside a lock"; its held-latch stack fills up
  (`stack_overflows`).
- In release builds the unlock functions do not get the pass value, so the
  hook in `rw_lock_*_unlock_func()` cannot tell a hand-off release.
- Threads polling io_fix (`buf_page_get_zip()`, buf0buf.cc:3617-3630) sleep
  with `std::this_thread::sleep_for`, which libcoz does not intercept, so the
  wait for another thread is invisible.

## To Be

- A latch acquired with pass != 0 is a hand-off latch: its acquisition ends
  the wait (wait stats, `post_block(true)`) but does not raise the thread's
  lock depth or push a held entry; its release (on any thread) only lets the
  releasing thread catch up (`catch_up()`) before waiters can wake, and does
  not change any thread's lock depth.
- Latch stats: waits for hand-off latches are recorded per site as usual; hand-off
  latches are excluded from hold statistics (the hold is I/O time, not
  critical-section time); counted as `handoff_acquires` / `handoff_releases`
  per snapshot.
- libcoz reports the current lock depth of threads (`lock_depth` section of
  the latch stats snapshot) so a stuck depth is visible.
- Polling waits for I/O completion are bracketed by pre_block / post_block.
- Kernel I/O waits (io_getevents, synchronous pread/pwrite) are not hand-offs:
  no skip; documented in os0file.cc.

## Requirements

1. libcoz exports `_coz_get_latch_api2` (v1 kept unchanged) with
   `acquired_handoff`, `release_handoff`, `pre_block`, `post_block`.
2. InnoDB (`ut0coz.h`) uses v2 when present; falls back to v1
   `acquired`/`release` with an old libcoz (never leaves a wait open).
3. Without libcoz loaded: one predictable branch per hook; WITH_COZ=OFF:
   nothing compiled.
4. Hand-off decision is made from the pass value in the rw-lock layer
   (lock functions and `*_unlock_gen` wrappers), not at BUF_IO call sites.

## Acceptance Criteria

- ctest `latch_stats` covers cross-thread hand-off release: no unmatched
  releases, no overflows, acquiring thread's held depth 0; same-thread
  hand-off acquire/release also balanced.
- All coz ctests and `server` pytest pass.
- IO-bound sysbench under libcoz with latch stats: `unmatched_releases` = 0,
  `stack_overflows` = 0, no thread with raised lock depth after the run;
  latch_top works; 0% experiment TPS overhead not worse than before.
- mysqld WITH_COZ=ON builds; MTR `main.1st` and a few innodb tests pass
  without libcoz.

## Testing Plan

- Unit: `tests/latch/latch_stats_test.cpp` new cases (handoff cross-thread,
  same-thread, lock depth report).
- Integration: smoke datadir `~/builds/8.4-coz/coz-smoke`, 64M buffer pool,
  sysbench oltp_read_only / oltp_read_write, before (saved binaries in
  `~/builds/8.4-coz/before/`) vs after.
- MTR on `~/builds/8.4-coz`.

## Implementation Plan

1. coz (`coz-mcp-aio-handoff`): registry handoff API + lock-depth report,
   libcoz v2 table, coz.h macros, tests.
2. PS (`PS-11508-8.4-coz-hooks`): ut0coz.h v2 resolution, pass-aware
   wait guard / acquired / release, release hook moved into the
   `*_unlock_gen` wrappers, io_fix poll hooks, os0file.cc comments.
3. coz-mcp (`aio-handoff`): submodule pointer, server field pass-through,
   docs/mysql.md notes.
