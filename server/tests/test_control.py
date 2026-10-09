import asyncio
import json

import pytest

from coz_mcp.control import CozControl, CozError, CozUnavailable


class FakeCoz:
    """Minimal JSON-lines server mimicking libcoz's control socket."""

    def __init__(self, path):
        self.path = path
        self.requests = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        while line := await reader.readline():
            req = json.loads(line)
            self.requests.append(req)
            if req["cmd"] == "status":
                resp = {"mode": "manual", "global_delay_ns": 42}
            elif req["cmd"] == "big":
                resp = {"lines": ["x" * 100 for _ in range(2000)]}
            elif req["cmd"] == "slow":
                await asyncio.sleep(0.3)
                resp = {"slow": True}
            elif req["cmd"] == "drop":
                writer.close()
                return
            else:
                resp = {"error": f"unknown cmd: {req['cmd']}"}
            try:
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()
            except ConnectionError:
                return  # client dropped the connection
        writer.close()


@pytest.fixture
async def fake(tmp_path):
    f = FakeCoz(tmp_path / "coz.sock")
    await f.start()
    yield f
    await f.stop()


@pytest.mark.anyio
async def test_request_round_trip(fake):
    c = CozControl(str(fake.path))
    assert (await c.request("status"))["global_delay_ns"] == 42
    assert await c.global_delay_ns() == 42
    assert fake.requests == [{"cmd": "status"}, {"cmd": "status"}]
    await c.close()


@pytest.mark.anyio
async def test_args_are_sent(fake):
    c = CozControl(str(fake.path))
    with pytest.raises(CozError, match="unknown cmd: start"):
        await c.request("start", line="a.cc:1", kind="speedup", percent=5)
    assert fake.requests[-1] == {
        "cmd": "start",
        "line": "a.cc:1",
        "kind": "speedup",
        "percent": 5,
    }
    await c.close()


@pytest.mark.anyio
async def test_reconnects_after_server_drops_connection(fake):
    c = CozControl(str(fake.path))
    await c.request("status")
    with pytest.raises(CozUnavailable):
        await c.request("drop")
    # Next request opens a fresh connection.
    assert (await c.request("status"))["mode"] == "manual"
    await c.close()


@pytest.mark.anyio
async def test_missing_socket_explains_likely_causes(tmp_path):
    c = CozControl(str(tmp_path / "nope.sock"))
    with pytest.raises(CozUnavailable) as e:
        await c.request("status")
    msg = str(e.value)
    assert "COZ_CONTROL_SOCKET" in msg
    assert "perf_event_paranoid" in msg


@pytest.mark.anyio
async def test_concurrent_requests_are_serialized(fake):
    c = CozControl(str(fake.path))
    results = await asyncio.gather(*(c.request("status") for _ in range(10)))
    assert all(r["global_delay_ns"] == 42 for r in results)
    await c.close()


@pytest.mark.anyio
async def test_reply_larger_than_64k(fake):
    c = CozControl(str(fake.path))
    assert len((await c.request("big"))["lines"]) == 2000
    assert (await c.request("status"))["mode"] == "manual"
    await c.close()


@pytest.mark.anyio
async def test_cancelled_request_does_not_desync_replies(fake):
    c = CozControl(str(fake.path))
    t = asyncio.create_task(c.request("slow"))
    await asyncio.sleep(0.05)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    # The late "slow" reply must not be returned for this request.
    assert (await c.request("status"))["mode"] == "manual"
    await c.close()
