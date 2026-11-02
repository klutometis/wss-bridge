"""--exit-on-auth-failure: a refused token ends the bridge (status 77)
instead of retrying every minute; without the flag nothing changes."""

import asyncio
import socket
import subprocess
import sys
import time

import pytest
import websockets
from websockets.asyncio.server import serve
from websockets.http11 import Response
from websockets.datastructures import Headers


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _refusing_gateway(port: int, status: int):
    def refuse(conn, request):
        return Response(status, "Refused", Headers(), b"")
    return serve(lambda ws: None, "127.0.0.1", port, process_request=refuse)


def _bridge(port: int, *flags: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "wss_bridge", "--wss-url", f"ws://127.0.0.1:{port}/bridge",
         "--token", "revoked", *flags, "--cmd", sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_token_ends_the_bridge_with_77(status):
    async def go():
        port = _free_port()
        async with _refusing_gateway(port, status):
            p = _bridge(port, "--exit-on-auth-failure")
            t = time.monotonic()
            while p.poll() is None and time.monotonic() - t < 15:
                await asyncio.sleep(0.1)
            return p
    p = asyncio.run(go())
    assert p.returncode == 77, p.stderr.read()


def test_without_the_flag_it_keeps_retrying():
    async def go():
        port = _free_port()
        async with _refusing_gateway(port, 403):
            p = _bridge(port)
            await asyncio.sleep(3)
            alive = p.poll() is None
            p.terminate(); p.wait(5)
            return alive
    assert asyncio.run(go())


def test_other_refusals_still_retry_with_the_flag():
    async def go():
        port = _free_port()
        async with _refusing_gateway(port, 502):   # a gateway restarting
            p = _bridge(port, "--exit-on-auth-failure")
            await asyncio.sleep(3)
            alive = p.poll() is None
            p.terminate(); p.wait(5)
            return alive
    assert asyncio.run(go())
