"""Ctrl-C (SIGINT) ends the bridge quietly: exit 130, no orphaned-task
traceback, within a few seconds, even when the gateway never answers the
close (a laptop whose network just went away)."""

import asyncio
import base64
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time

from websockets.asyncio.server import serve


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


# The helper dies of SIGINT without a traceback of its own.
CHILD = "import signal, time; signal.signal(signal.SIGINT, signal.SIG_DFL); time.sleep(60)"


async def _mute_gateway(port: int, connected: asyncio.Event):
    """Completes the WebSocket upgrade, then never sends another byte."""
    async def conn(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = next(l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.lower().startswith("sec-websocket-key:"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: mcp\r\n\r\n").encode())
        await writer.drain()
        connected.set()
        while await reader.read(4096):
            pass
    return await asyncio.start_server(conn, "127.0.0.1", port)


import pytest


@pytest.mark.parametrize("gateway", ["answers the close", "never answers"])
def test_ctrl_c_exits_quietly(gateway):
    async def go():
        port = _free_port()
        connected = asyncio.Event()

        async def polite(ws):
            connected.set()
            async for _ in ws:
                pass

        server = (await serve(polite, "127.0.0.1", port, subprotocols=["mcp"]) if gateway == "answers the close"
                  else await _mute_gateway(port, connected))
        try:
            p = subprocess.Popen(
                [sys.executable, "-m", "wss_bridge", "--wss-url", f"ws://127.0.0.1:{port}/bridge",
                 "--token", "t", "--cmd", sys.executable, "-c", CHILD],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, start_new_session=True)
            await asyncio.wait_for(connected.wait(), 15)
            await asyncio.sleep(0.5)
            t = time.monotonic()
            os.killpg(p.pid, signal.SIGINT)          # Ctrl-C hits the whole foreground group
            while p.poll() is None and time.monotonic() - t < 20:
                await asyncio.sleep(0.1)
            took = time.monotonic() - t
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL); p.wait()
            return p, took
        finally:
            server.close()   # not wait_closed(): the mute gateway's reader would wait on us

    p, took = asyncio.run(go())
    err = p.stderr.read()
    assert p.returncode == 130, (p.returncode, err[-500:])
    assert "Task exception was never retrieved" not in err, err[-800:]
    assert "Traceback" not in err, err[-800:]
    assert took < 5, (took, err[-500:])
