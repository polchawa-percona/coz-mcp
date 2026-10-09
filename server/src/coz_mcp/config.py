"""Server configuration from environment variables (set in the MCP client config).

COZ_MCP_SOCKET        control socket of the target (default /tmp/coz-mcp.sock)
COZ_MCP_WORKLOAD      "demo" (default) or "sysbench"
COZ_MCP_BUILD_DIR     coz-mcp build dir with demo/demo and coz/libcoz/libcoz.so
COZ_MCP_DEMO_ARGS     extra base args for the demo (space separated)
COZ_MCP_SYSBENCH      sysbench binary (default "sysbench")
COZ_MCP_SYSBENCH_ARGS base args, e.g. "oltp_read_write --mysql-socket=... run"
COZ_MCP_CLIENT_CPUS   taskset CPU list for the benchmark client (sysbench)
COZ_MCP_PROFILE       Coz profile output for the demo (default /tmp/coz-mcp-demo.coz)
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .workload import WorkloadConfig

DEFAULT_SOCKET = "/tmp/coz-mcp.sock"


@dataclass(frozen=True)
class ServerConfig:
    control_socket: str
    workload: WorkloadConfig


def coz_env(build_dir: str | Path, control_socket: str, profile: str,
            source_scope: str = "%") -> dict[str, str]:
    """Environment that runs a program under the coz-mcp libcoz in manual mode."""
    lib = Path(build_dir) / "coz" / "libcoz" / "libcoz.so"
    return {
        "LD_PRELOAD": str(lib),
        "COZ_BINARY_SCOPE": "MAIN",
        "COZ_SOURCE_SCOPE": source_scope,
        "COZ_PROGRESS_POINTS": "",
        "COZ_OUTPUT": profile,
        "COZ_OUTPUT_FORMAT": "json",
        "COZ_CONTROL_SOCKET": control_socket,
    }


def demo_workload(build_dir: str | Path, control_socket: str, profile: str,
                  base_args: list[str] | None = None) -> WorkloadConfig:
    """The demo is both the profiled target and the benchmark."""
    return WorkloadConfig(
        name="demo",
        binary=str(Path(build_dir) / "demo" / "demo"),
        base_args=list(base_args or []),
        report_args=[],
        env=coz_env(build_dir, control_socket, profile),
    )


def sysbench_workload(binary: str, base_args: list[str],
                      cpus: str | None = None) -> WorkloadConfig:
    """sysbench drives a target (mysqld) that was started under coz separately."""
    return WorkloadConfig(
        name="sysbench",
        binary=binary,
        base_args=base_args,
        report_args=["--report-interval=1"],
        cpus=cpus,
    )


def from_env(environ: dict[str, str] | None = None) -> ServerConfig:
    env = dict(os.environ if environ is None else environ)
    socket = env.get("COZ_MCP_SOCKET", DEFAULT_SOCKET)
    kind = env.get("COZ_MCP_WORKLOAD", "demo")
    if kind == "demo":
        build_dir = env.get("COZ_MCP_BUILD_DIR")
        if not build_dir:
            raise ValueError("COZ_MCP_BUILD_DIR is required for the demo workload")
        workload = demo_workload(
            build_dir, socket,
            env.get("COZ_MCP_PROFILE", "/tmp/coz-mcp-demo.coz"),
            shlex.split(env.get("COZ_MCP_DEMO_ARGS", "")),
        )
    elif kind == "sysbench":
        workload = sysbench_workload(
            env.get("COZ_MCP_SYSBENCH", "sysbench"),
            shlex.split(env.get("COZ_MCP_SYSBENCH_ARGS", "")),
            env.get("COZ_MCP_CLIENT_CPUS") or None,
        )
    else:
        raise ValueError(f"unknown COZ_MCP_WORKLOAD: {kind!r} (demo or sysbench)")
    return ServerConfig(control_socket=socket, workload=workload)
