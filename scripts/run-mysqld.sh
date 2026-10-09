#!/bin/bash
# Run mysqld from a Percona Server build with InnoDB latch hooks (-DWITH_COZ=ON,
# branch PS-11508-8.4-coz-hooks) under coz-mcp's libcoz, pinned to CPUS.
# taskset and env start without LD_PRELOAD; only the final exec of mysqld
# loads libcoz. See docs/mysql.md.
#
#   WORKDIR=~/coz-mysql scripts/run-mysqld.sh [--no-coz] [mysqld args...]
#
# WORKDIR must contain my.cnf; the control socket is $WORKDIR/coz.sock.
set -euo pipefail
WORKDIR=${WORKDIR:?set WORKDIR to the directory with my.cnf}
BUILD=${BUILD:-$HOME/builds/8.4-coz}
SRC_SCOPE=${SRC_SCOPE:-%/storage/innobase/%}
LIBCOZ=${LIBCOZ:-$HOME/builds/coz-mcp-release/coz/libcoz/libcoz.so}
CPUS=${CPUS:-0-7}
SOCK=${COZ_SOCK:-$WORKDIR/coz.sock}
MYSQLD="$BUILD/runtime_output_directory/mysqld"

if [[ "${1:-}" == "--no-coz" ]]; then
  shift
  exec taskset -c "$CPUS" "$MYSQLD" --defaults-file="$WORKDIR/my.cnf" "$@"
fi

exec taskset -c "$CPUS" env \
  LD_PRELOAD="$LIBCOZ" \
  COZ_BINARY_SCOPE=MAIN \
  COZ_SOURCE_SCOPE="$SRC_SCOPE" \
  COZ_PROGRESS_POINTS= \
  COZ_OUTPUT="$WORKDIR/profile.coz" \
  COZ_OUTPUT_FORMAT=json \
  COZ_CONTROL_SOCKET="$SOCK" \
  "$MYSQLD" --defaults-file="$WORKDIR/my.cnf" "$@"
