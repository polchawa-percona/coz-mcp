# coz-mcp

An MCP server that lets an AI agent (e.g. Claude Code) find performance
bottlenecks in a running program by experimenting on it while a benchmark
runs. It drives a fork of the [Coz](https://github.com/plasma-umass/coz)
causal profiler loaded into the target process:

- **slowdown** a source line by N% and measure the TPS change: does this line
  limit throughput?
- **virtual speedup** of a source line by N% (Coz): how much faster would the
  program be if this line were optimized?
- **hot lines**: where the target spends CPU right now (candidates);
- **latch statistics**: rank mutex / rw-latch acquire sites (`file:line`) by
  wait and hold time, live.

Results are measured on the benchmark's own TPS (sysbench report lines),
corrected for Coz virtual delays, with alternating baselines and a 95%
confidence interval. Work tracked as PS-11508.

See [docs/architecture.md](docs/architecture.md) for how it works, including
a walkthrough of the MCP concepts used (tools, progress, resources,
subscriptions, prompts, lifespan, in-memory testing).

## Layout

- `coz/` - Coz fork (submodule, branch `coz-mcp-control`): control socket,
  manual mode, slowdown, latch hooks and statistics, accuracy fixes.
- `server/` - Python MCP server (`coz_mcp`, MCP Python SDK v2, stdio).
- `demo/` - workload with a known lock bottleneck, used for validation.
- `scripts/validate_demo.py` - compares experiments with real code changes.
- `scripts/run-demo.sh` - run the demo under libcoz by hand.

## Build and test

Requirements: Linux, CMake, Ninja, a C++17 compiler, Python 3.12 and
[uv](https://docs.astral.sh/uv/), and `kernel.perf_event_paranoid <= 1`:

```
sudo sysctl kernel.perf_event_paranoid=1
git submodule update --init
cmake -S . -B ~/builds/coz-mcp-release -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
ninja -C ~/builds/coz-mcp-release
(cd ~/builds/coz-mcp-release && ctest)
cd server && uv run pytest            # unit + integration (demo under libcoz)
uv run python ../scripts/validate_demo.py
```

## Use from Claude Code

Add to `.mcp.json` (project) or with `claude mcp add`:

```json
{
  "mcpServers": {
    "coz-mcp": {
      "command": "uv",
      "args": ["--directory", "/home/you/Projects/coz-mcp/server", "run", "coz-mcp"],
      "env": {
        "COZ_MCP_WORKLOAD": "demo",
        "COZ_MCP_BUILD_DIR": "/home/you/builds/coz-mcp-release",
        "COZ_MCP_DEMO_CPUS": "8-13",
        "COZ_MCP_SOCKET": "/tmp/coz-mcp.sock"
      }
    }
  }
}
```

With `COZ_MCP_WORKLOAD=demo` the server starts the demo under libcoz itself
(`start_workload`). For a separately started target such as mysqld, use
`COZ_MCP_WORKLOAD=sysbench`, `COZ_MCP_SYSBENCH_ARGS` (e.g.
`"oltp_read_write --tables=8 --table-size=100000 --mysql-socket=... run"`)
and `COZ_MCP_CLIENT_CPUS` (CPUs not used by mysqld). All variables are listed
in `server/src/coz_mcp/config.py`.

Then ask, for example: *"Use coz-mcp to find what limits the demo's TPS"*, or
pick the `find_bottleneck` prompt.

## Instrumenting your own locks

Coz interposes pthread mutexes, rw-locks and condition variables. For custom
locks (e.g. InnoDB latches), call the hooks from `coz/include/coz.h`:

```c
COZ_LATCH_WAIT_BEGIN(&lock, __FILE__, __LINE__, "my_lock", COZ_LATCH_X);
acquire(&lock);
COZ_LATCH_ACQUIRED(&lock);
...
COZ_LATCH_RELEASE(&lock);
release(&lock);
```

They make slowdowns land inside the critical section, keep virtual delays out
of it, and feed `latch_top`. Without libcoz loaded they cost one branch.
Measured on the demo (single thread, estimates): ~0 ns per lock/unlock pair
without libcoz, ~7 ns with libcoz idle, ~75 ns with latch statistics on.

## Validation (demo, 4 threads, pinned)

Each experiment is compared with the real change of the same code measured in
the same session (`scripts/validate_demo.py`):

| case | coz-mcp | real change |
|---|---|---|
| slowdown 50% critical section, pthread | -39.7% | -30.9% |
| slowdown 50% parallel code, pthread | -6.8% | -4.9% |
| speedup 30% critical section, pthread | +29.9% | +40.9% |
| slowdown 50% critical section, hooked spin lock | -30.9% | -29.7% |
| speedup 30% critical section, hooked spin lock | +30.5% | +45.7% |
| slowdown 50% critical section, spin lock without hooks | -8.4% | -29.6% |

Slowdowns match the real change closely. Speedup predictions point the right
way but under-estimate the gain by roughly a quarter to a third (the last
checked case is at the edge of the tolerance; an earlier run gave +35%).
The last row shows why custom locks need the hooks. With upstream Coz
semantics (`COZ_LOCK_AWARE_DELAYS=0 COZ_SAMPLE_EVENT=task-clock`) the first
three rows were -9%, -65% and -4%.
