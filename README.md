# coz-mcp

MCP server that lets an AI agent drive [Coz](https://github.com/plasma-umass/coz)
causal-profiling experiments at runtime, while a benchmark (sysbench or the bundled
demo) is running:

- virtual **speedup** of a source line (Coz) and real **slowdown** of a source line,
- live TPS from the benchmark, corrected for Coz virtual delays,
- wait/hold time ranking of mutex and rw-latch acquire sites (with latch hooks).

Status: work in progress (PS-11508). See `.requirements/` for the design.

## Layout

- `coz/` - Coz fork (submodule, branch `coz-mcp-control`) with runtime control.
- `server/` - Python MCP server (`coz_mcp`).
- `demo/` - workload with a known bottleneck, used for validation.

## Build

```
git submodule update --init
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo
ninja -C build
cd server && uv run pytest
```

Coz needs `kernel.perf_event_paranoid <= 1`.
