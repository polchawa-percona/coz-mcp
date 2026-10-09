# coz-mcp architecture

This document explains how coz-mcp is put together, with a focus on the
Model Context Protocol (MCP) concepts it uses, so it can be read as a worked
example of an MCP server.

## Big picture

```
 Claude Code (MCP client)
        |  stdio, JSON-RPC (MCP)
        v
 coz-mcp server (Python, server/src/coz_mcp)
   |  tools / resources / prompts
   |-- Workload: runs sysbench or the demo, parses "[ Ns ] ... tps: X" lines
   |-- CozControl: unix socket, JSON lines
        v
 target process (mysqld or demo) with LD_PRELOAD=libcoz.so (Coz fork)
   |-- control socket thread (profiler thread in manual mode)
   |-- perf sampling (ref-cycles) of every thread
   |-- virtual speedup / real slowdown of one source line
   |-- latch hooks (COZ_LATCH_*): delays at lock transitions, wait/hold stats
```

The model never talks to the target directly. It calls MCP tools; the server
turns them into control-socket commands and benchmark runs, and returns
structured results.

## MCP concepts used

### Server and transport

`build_server()` in `server.py` creates an `MCPServer` (MCP Python SDK v2,
formerly called FastMCP). `main()` runs it over **stdio**: the MCP client
(Claude Code) starts `coz-mcp` as a subprocess and exchanges JSON-RPC messages
over stdin/stdout. Nothing else may be printed to stdout, which is why the
benchmark's output is captured by the `Workload` class and never echoed.

The server is configured through environment variables (`config.py`), which
the MCP client sets from its `.mcp.json` entry.

### Tools

A tool is a function the model can call. With the SDK, a decorated Python
function is enough:

```python
@mcp.tool()
async def hot_lines(window_s: float = 5, limit: int = 20,
                    path_filter: str | None = None) -> dict[str, Any]:
    """Source lines where the target spent CPU during the last window_s seconds."""
```

- The **type hints become the JSON input schema** the model sees, and the SDK
  validates arguments against it (`Literal["speedup", "slowdown"]` becomes an
  enum).
- The **docstring becomes the tool description**. It is effectively prompt
  text for the model, so it explains when and how to use the tool, not how it
  is implemented.
- A returned `dict` becomes **structured content** (`structured_content` on
  the client side) plus a text rendering.
- Raising `ToolError` returns an error result whose message the model sees
  (e.g. "no workload running; call start_workload first"). Any other
  exception is reported without details, so expected failures are always
  converted to `ToolError`.

Tools here: `coz_status`, `start_workload`, `stop_workload`, `workload_tps`,
`hot_lines`, `run_experiment`, `latch_stats_enable`, `latch_stats_reset`,
`latch_top`.

Design choices worth noting:
- The model can only append arguments to a benchmark command configured by
  the operator; it cannot choose the binary. Tools are an attack surface:
  keep them as narrow as the task allows.
- `run_experiment` refuses to run concurrently with another experiment. MCP
  clients may issue tool calls in parallel; shared state needs explicit
  guarding (`asyncio.Lock`).

### Progress notifications

`run_experiment` takes a few minutes. The tool declares a `ctx: Context`
parameter (hidden from the model's schema) and calls
`await ctx.report_progress(i, total, message)` before every run. Clients show
this as a progress bar; tests receive it through `progress_callback`.

### Resources

Resources are read-only data addressed by URIs, which a client can show to the
user or attach to the model's context without a tool call:

- `coz://experiments` - all experiment results of the session,
- `coz://hot-lines` - the latest hot-lines ranking,
- `coz://latches/top` - the latest latch wait/hold rankings.

### Subscriptions

With the 2026-07-28 protocol revision, clients open one `subscriptions/listen`
stream and name the resource URIs they care about. The server publishes
`ResourceUpdated` events on a subscription bus. coz-mcp creates its own
`InMemorySubscriptionBus`, passes it to `MCPServer(subscriptions=bus)`, and
publishes from a background task (`latch_poller`) that refreshes
`coz://latches/top` every few seconds while latch statistics are on - so a
client sees contention change live, without polling a tool.

### Prompts

`find_bottleneck` is a prompt template: a parameterized message the user can
pick in the client ("/find_bottleneck" style) that tells the model how to use
the tools together (hot lines -> slowdown screening -> speedup prediction ->
report).

### Lifespan

The `lifespan` async context manager runs around the whole server. coz-mcp
starts the latch poller there and, on shutdown, stops a running experiment
(so the target is never left slowed down) and the benchmark process.

### Testing an MCP server

`mcp.Client(server)` connects to a server object **in memory** - no
subprocess, no JSON-RPC framing - and is how `tests/test_server.py` drives
every tool, resource, prompt and subscription against fake Coz and workload
objects. `tests/integration/` then runs the same tools against the real demo
under libcoz.

## The Coz fork

Coz (plasma-umass/coz) is a causal profiler: it predicts how much a program
would speed up if one source line were faster, by pausing all other threads
whenever the selected line runs ("virtual speedup"). coz-mcp's fork adds:

1. **Control socket and manual mode** (`libcoz/control.*`): with
   `COZ_CONTROL_SOCKET`, the profiler thread serves JSON-line commands
   (`status`, `hot_lines`, `start`, `stop`, `latch_stats*`) instead of running
   random experiments, and does not need progress points (throughput comes
   from the benchmark). Experiments auto-stop after `max_duration_s`.
2. **Slowdown experiments**: the thread that runs the selected line really
   becomes slower; its own sleep is not virtual delay.
3. **Latch hooks** (`COZ_LATCH_*` in `coz.h`) for locks Coz cannot interpose
   (InnoDB mutexes and rw-latches are inline code).
4. **Latch statistics** (`libcoz/latch_stats.*`): per acquire site wait/hold
   counts, sums, maxima and log2 histograms, per-thread counters.
5. **Accuracy fixes** found by validating against real code changes
   (`scripts/validate_demo.py`):
   - lock-aware delays: virtual delays are never paid while holding a lock,
     and samples are drained before each acquire;
   - slowdown paid in proportion to each critical section (percent% of its
     length), not in 0.5 ms lumps;
   - sampling with `ref-cycles` instead of the software task clock, which
     under-sampled code just before a lock handoff by ~5x.

`COZ_LOCK_AWARE_DELAYS=0 COZ_SAMPLE_EVENT=task-clock` restores upstream
behaviour.

Option `COZ_PAUSE_OVERSHOOT=bank` (default `propagate`, as upstream): a
thread credits only the delay it needed and pays its next delays from the
time nanosleep over-slept. Upstream credits the whole sleep, which puts the
thread ahead, so every other thread pauses for the overshoot and overshoots
in turn; on the 4-thread demo the global delay grows 5.7x faster than the
samples justify and the program runs at half speed during a speedup
experiment. Bank keeps the global delay at the sampled credit, but its
speedup predictions on the demo were 5-10 points below the real change
measured under libcoz, so it is not the default yet.

The `stop` reply carries `delay_counters` (credit from samples, global delay
pushed, pauses, overshoot, deferred payments, skipped delays), which
`run_experiment` returns per run.

Known limitation: the per-thread hardware perf event used for sampling can
change the program. On an AMD Zen5 host it slowed the demo with a 30% faster
critical section by 12-17% but the unchanged demo by only ~3% (also with
`perf stat -e ref-cycles` and no libcoz), so the real gain of that change is
~29% under libcoz versus ~41% without it. Coz predicts the instrumented
program; `scripts/validate_demo.py` reports both ground truths.

## Throughput measurement

Coz normally counts progress points inside the program. coz-mcp instead uses
the benchmark's per-second report lines, sampling Coz's global delay counter
at each line. For a speedup experiment each interval is corrected by the
virtual delay inserted during it:
`corrected_tps_i = txns_i / (len_i - delay_i)` (clamped at 10% of `len_i`),
see `metrics.py`. Slowdown experiments need no correction. Each experiment
alternates 0% baseline and target runs on the same line, and `stats.py`
reports the delta with a Welch 95% confidence interval.

Known limitation: the benchmark client runs outside Coz's virtual time, so it
must run on CPUs not used by the target, and open-loop load (`--rate`) is
rejected.
