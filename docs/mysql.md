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

Known limitations:
- Latches released by another thread (e.g. page latches released by I/O
  completion) show up as `unmatched_releases` and leave the owner's lock
  depth counted as held.
- Waits built on raw atomics instead of InnoDB latches or pthread primitives
  are not seen by the hooks.
- Hook cost while latch statistics are on is noticeable under heavy load
  (about -14% TPS in the smoke test, estimate); baselines and experiments
  pay it equally.
