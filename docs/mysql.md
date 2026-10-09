# Percona Server with coz-mcp

## 1. Build Percona Server with latch hooks

The InnoDB mutex and rw-latch hooks live on Percona Server branch
`PS-11508-8.4-coz-hooks` (`storage/innobase/include/ut0coz.h`, compiled only
with `-DWITH_COZ=ON`). Frame pointers give Coz usable call chains.

```
cmake <ps-src> -G Ninja -B ~/builds/8.4-coz -DCMAKE_BUILD_TYPE=RelWithDebInfo \
  -DWITH_COZ=ON -DWITH_ROCKSDB=OFF \
  -DCMAKE_C_FLAGS=-fno-omit-frame-pointer -DCMAKE_CXX_FLAGS=-fno-omit-frame-pointer \
  <usual options: -DWITH_BOOST=..., -DDOWNLOAD_BOOST=ON, ...>
ninja -C ~/builds/8.4-coz mysqld
```

Without the hooks coz-mcp still works for code outside InnoDB latches, but a
slowdown of a line inside an InnoDB critical section is applied outside it
(see the "spin lock without hooks" row in the README validation table).

## 2. Data directory and config

```
mkdir -p ~/coz-mysql && cd ~/coz-mysql
cat > my.cnf <<CNF
[mysqld]
datadir=$HOME/coz-mysql/data
socket=$HOME/coz-mysql/mysql.sock
port=3399
mysqlx=OFF
log-error=$HOME/coz-mysql/mysqld.err
innodb_buffer_pool_size=4G
innodb_redo_log_capacity=4G
innodb_flush_log_at_trx_commit=2
skip-log-bin
max_connections=512
CNF
~/builds/8.4-coz/runtime_output_directory/mysqld --defaults-file=my.cnf --initialize-insecure
```

## 3. Prepare sysbench data (without coz)

```
WORKDIR=~/coz-mysql scripts/run-mysqld.sh --no-coz &
mysql -uroot -S ~/coz-mysql/mysql.sock -e "CREATE DATABASE sbtest"
taskset -c 8-15 sysbench oltp_read_write --mysql-socket=$HOME/coz-mysql/mysql.sock \
  --mysql-user=root --tables=8 --table-size=100000 --threads=8 prepare
mysqladmin -uroot -S ~/coz-mysql/mysql.sock shutdown
```

## 4. Run mysqld under libcoz

```
WORKDIR=~/coz-mysql CPUS=0-7 scripts/run-mysqld.sh &
```

`COZ_SOURCE_SCOPE` defaults to `%/storage/innobase/%`, so hot lines and
experiments are limited to InnoDB (and parsing the debug info stays fast).
Set `SRC_SCOPE=%` for the whole server.

## 5. Configure coz-mcp for sysbench

```json
{
  "mcpServers": {
    "coz-mcp": {
      "command": "uv",
      "args": ["--directory", "/home/you/Projects/coz-mcp/server", "run", "coz-mcp"],
      "env": {
        "COZ_MCP_WORKLOAD": "sysbench",
        "COZ_MCP_SOCKET": "/home/you/coz-mysql/coz.sock",
        "COZ_MCP_CLIENT_CPUS": "8-15",
        "COZ_MCP_SYSBENCH_ARGS": "oltp_read_write --mysql-socket=/home/you/coz-mysql/mysql.sock --mysql-user=root --tables=8 --table-size=100000 --threads=64 --time=3600 run"
      }
    }
  }
}
```

Keep mysqld and sysbench on disjoint CPUs (and avoid SMT siblings of the
mysqld CPUs): the sysbench client runs outside Coz's virtual time.

## What to expect

From a smoke test (oltp_read_write, 64 threads, busy machine, so TPS numbers
are noisy):

- `hot_lines`: `os0event.ic`, `sync0rw.ic`, `trx0trx.h`, `ut0ut.cc` (spin
  delay), `row0sel.cc`, `rem0cmp.cc`.
- `latch_top` hold: index tree and buffer block S-latches in `btr0cur.cc`,
  `row0sel.cc`, `row0upd.cc`; the log writer mutex in `log0write.cc` with a
  long average hold.
- `latch_top` wait: buffer block latches (`row0sel.cc`, `row0row.cc`,
  `btr0cur.cc`), the page hash latch (`buf0buf.cc`), the trx mutex.

Experiment overhead on mysqld (oltp_read_write, 64 threads, quiet machine,
single runs, estimates): 13,578 TPS without an experiment, 12,330 TPS during
a 0% experiment (the cost of draining samples at latch transitions; baselines
pay it too), 10,789 TPS with a 50% slowdown of `rem0cmp.cc:622` (record
comparison during B-tree search).

Known limitations:
- Waits that only sleep and poll are not seen, e.g. `buf_LRU_get_free_block()`
  when the free list is empty (page cleaner or the thread's own single-page
  flush; `Innodb_buffer_pool_wait_free` grew by about 10,000 per minute in
  the 64M buffer pool smoke test below, estimate).
- Waits built on raw atomics instead of InnoDB latches or pthread primitives
  are not seen by the hooks.
- Hook cost while latch statistics are on is noticeable under heavy load
  (about -14% TPS in the smoke test, estimate); baselines and experiments
  pay it equally.

## Asynchronous I/O hand-offs

InnoDB hands block latches over to asynchronous I/O with a non-zero pass value
(page reads: X latch with `BUF_IO_READ`, page writes: SX latch with
`BUF_IO_WRITE`); the I/O handler thread releases them when the I/O completes.
The hooks on `PS-11508-8.4-coz-hooks` report every latch acquired with a
non-zero pass value as a hand-off latch through libcoz's v2 hook table
(`_coz_get_latch_api2`, coz branch `coz-mcp-aio-handoff`):

- the acquisition ends the latch wait as usual but does not make the thread a
  lock holder (its virtual delays are not deferred);
- the release, on whichever thread, lets that thread catch up on Coz delays
  before waiters wake and does not change any lock depth;
- latch statistics record the wait at the acquire site but no hold time (the
  time until release is I/O time); `latch_top` reports
  `handoff_acquires_per_s`, `held_now`, `lock_depth_raised` and
  `lock_depth_max`.

Threads waiting for a page read wait on the block latch (`buf_wait_for_read`)
and are bracketed by the latch hooks; the io_fix polling loop of
`buf_page_get_zip` is bracketed with pre_block/post_block. The I/O threads'
`io_getevents()` wait is deliberately not: it is ended by the device, which
runs outside Coz virtual time. With an older libcoz (v1 table only) hand-off
latches are reported like other latches.

`lock_depth_raised` counts threads with a non-zero depth at the snapshot.
The `pthread_mutex_lock` wrapper raises the depth and the `pthread_cond_wait`
wrapper leaves it alone (libcoz.cpp), so threads parked in InnoDB `os_event`
waits count as holding the event mutex: an idle server shows about 30 threads
with depth 1. A missed release instead grows a thread's depth without bound:
the old hooks (mysqld before the change) under the new libcoz left
`lock_depth_max` at about 104,000 at idle (`held_now` 1,155), the new hooks at
1 (`held_now` 0). The new mysqld under the old libcoz (v1 fallback) runs and
behaves as before (about 1M unmatched releases).

Smoke test (8 x 100k rows, `--innodb-buffer-pool-size=64M`, sysbench 32
threads, mysqld on 8 CPUs, busy shared machine, single runs, estimates):

| | before | after | after (libcoz merged with 5ffaad5) |
|---|---|---|---|
| unmatched releases / stack overflows after read_write | 1.58M / 1.58M | 0 / 0 | 0 / 0 |
| hand-off acquires = releases (after idle) | n/a | 1.03M = 1.03M | 1.02M = 1.02M |
| held latches at idle / max lock depth at idle | n/a | 0 / 1 | 0 / 1 |
| oltp_read_only TPS, no experiment | 19,900 / 19,732 | 19,904 / 20,203 | 20,071 / 18,159 |
| oltp_read_only TPS, 0% experiment | 18,316 / 17,752 | 18,296 / 17,691 | 17,943 / 16,907 |

The 0% experiment costs about 9-10% TPS in all three; the differences are
within the noise of the shared machine.

Read-only runs had no unmatched releases even before: single-page reads are
synchronous, so the same thread acquires and releases the latch; the
unmatched releases came from page writes (page cleaner SX latches released by
write completion). Before the change those reads also showed up as long
holds of `buf0buf.cc:5261` (the I/O time); they are now waits only.
