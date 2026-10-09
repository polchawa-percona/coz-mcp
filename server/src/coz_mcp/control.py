"""Client for the libcoz control socket (JSON lines, one request at a time)."""

from __future__ import annotations

import asyncio
import json
from typing import Any


class CozError(Exception):
    """libcoz answered with {"error": ...}."""


class CozUnavailable(Exception):
    """The control socket cannot be reached."""


_HINT = (
    "Is the target running under coz with COZ_CONTROL_SOCKET set to this path? "
    "If coz failed to start sampling, check that kernel.perf_event_paranoid <= 1."
)


class CozControl:
    def __init__(self, path: str, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(self.path)
        except OSError as e:
            raise CozUnavailable(f"cannot connect to {self.path}: {e}. {_HINT}") from e

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None

    async def request(self, cmd: str, **args: Any) -> dict[str, Any]:
        payload = json.dumps({"cmd": cmd, **args}) + "\n"
        async with self._lock:
            if self._writer is None:
                await self._connect()
            assert self._reader is not None and self._writer is not None
            try:
                self._writer.write(payload.encode())
                await self._writer.drain()
                line = await asyncio.wait_for(self._reader.readline(), self.timeout)
            except (OSError, asyncio.TimeoutError) as e:
                await self.close()
                raise CozUnavailable(f"control socket {self.path} failed: {e!r}") from e
            if not line:
                await self.close()
                raise CozUnavailable(f"control socket {self.path} closed the connection")
        resp = json.loads(line)
        if "error" in resp:
            raise CozError(resp["error"])
        return resp

    async def global_delay_ns(self) -> int:
        return int((await self.request("status"))["global_delay_ns"])
