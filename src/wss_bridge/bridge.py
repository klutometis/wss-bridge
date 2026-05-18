"""The bridge loop: dial WSS, spawn subprocess, pump JSON-RPC frames.

The bridge is MCP-unaware. It assumes the subprocess speaks line-
delimited JSON on stdio (the standard MCP stdio transport convention)
and forwards each line as one text frame on the WebSocket.

Reconnect: on any disconnect (WSS close, subprocess exit, exception),
kill the subprocess if still alive, sleep with exponential backoff
(1s -> 60s cap), restart both. State is not preserved across
reconnect — MCP `initialize` runs afresh on each new WSS.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from typing import Sequence

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.typing import Subprotocol

log = logging.getLogger("wss_bridge")


async def _pump_stdout_to_ws(proc: asyncio.subprocess.Process, ws) -> None:
    """Read newline-delimited JSON from subprocess stdout, send each as a WS text frame."""
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            log.info("subprocess stdout closed; ending stdout->ws pump")
            return
        text = line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not text:
            continue
        await ws.send(text)


async def _pump_ws_to_stdin(ws, proc: asyncio.subprocess.Process) -> None:
    """Read each WS text frame, write to subprocess stdin with a trailing newline."""
    assert proc.stdin is not None
    async for message in ws:
        if isinstance(message, bytes):
            # MCP frames are text; ignore unexpected binary
            log.warning("dropping unexpected binary frame (%d bytes)", len(message))
            continue
        proc.stdin.write(message.encode("utf-8"))
        proc.stdin.write(b"\n")
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            log.info("subprocess stdin closed; ending ws->stdin pump")
            return
    log.info("WS closed; ending ws->stdin pump")


async def _pump_stderr(proc: asyncio.subprocess.Process) -> None:
    """Forward subprocess stderr to our own stderr so we can see helper errors in logs."""
    assert proc.stderr is not None
    while True:
        line = await proc.stderr.readline()
        if not line:
            return
        sys.stderr.write("[child] " + line.decode("utf-8", errors="replace"))
        sys.stderr.flush()


async def _run_once(
    wss_url: str,
    token: str,
    cmd: Sequence[str],
    env: dict[str, str] | None = None,
) -> None:
    """One connect-and-pump cycle. Returns on disconnect; caller handles reconnect."""
    log.info("dialing %s", wss_url)
    extra_headers = {"Authorization": f"Bearer {token}"}
    # websockets >=14 renamed extra_headers -> additional_headers
    async with ws_connect(
        wss_url,
        subprotocols=[Subprotocol("mcp")],
        additional_headers=extra_headers,
        ping_interval=20,
        ping_timeout=20,
    ) as ws:
        log.info("WSS connected; spawning subprocess: %s", " ".join(cmd))
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else os.environ.copy(),
        )
        try:
            done, pending = await asyncio.wait(
                {
                    asyncio.create_task(_pump_stdout_to_ws(proc, ws), name="stdout->ws"),
                    asyncio.create_task(_pump_ws_to_stdin(ws, proc), name="ws->stdin"),
                    asyncio.create_task(_pump_stderr(proc), name="stderr->log"),
                },
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                if task.exception():
                    log.warning("pump task %s exited with %s", task.get_name(), task.exception())
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            if proc.returncode is None:
                log.info("terminating subprocess pid=%s", proc.pid)
                try:
                    proc.send_signal(signal.SIGTERM)
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    log.warning("subprocess didn't exit on SIGTERM; killing")
                    proc.kill()
                    await proc.wait()
            log.info("subprocess exited with code %s", proc.returncode)


async def run(
    wss_url: str,
    token: str,
    cmd: Sequence[str],
    env: dict[str, str] | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
) -> None:
    """Main loop: reconnect ruthlessly with exponential backoff."""
    backoff = initial_backoff
    while True:
        try:
            await _run_once(wss_url, token, cmd, env)
            # Clean disconnect — reset backoff
            log.info("clean disconnect; reconnecting immediately")
            backoff = initial_backoff
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt; exiting")
            return
        except websockets.exceptions.InvalidStatus as e:
            # Auth failed or path wrong — terminal, but we still loop
            # in case the gateway is briefly misconfigured. Long sleep.
            log.error(
                "WSS rejected (HTTP %s); will retry in %ds",
                e.response.status_code, max_backoff,
            )
            await asyncio.sleep(max_backoff)
        except Exception as e:
            log.warning("bridge cycle failed: %s; reconnecting in %.1fs",
                        type(e).__name__ + ": " + str(e), backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
