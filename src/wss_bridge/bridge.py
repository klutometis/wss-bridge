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
import ssl
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


def _tls_context(wss_url: str) -> ssl.SSLContext | None:
    """Verify the gateway's certificate with the operating system's trust store.

    Python's own default depends on the interpreter: a python.org build on
    macOS trusts nothing until its "Install Certificates" step has run, and
    any build obeys an SSL_CERT_FILE left pointing at the wrong bundle. Both
    fail as "unable to get local issuer certificate" while curl and Chrome on
    the same Mac connect fine (Krish, 2026-11-02). truststore asks macOS
    (Keychain), Windows or the Linux system store instead, so the bridge
    trusts exactly what the rest of the machine trusts, MDM-installed roots
    included.
    """
    if not wss_url.startswith("wss://"):
        return None
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception as e:  # missing or unusable: fall back to Python's defaults
        log.warning("OS trust store unavailable (%s); using Python's certificate defaults", e)
        return ssl.create_default_context()


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
        ssl=_tls_context(wss_url),
        subprotocols=[Subprotocol("mcp")],
        additional_headers=extra_headers,
        ping_interval=20,
        ping_timeout=20,
        # How long to wait for the gateway to answer our close. The default
        # (10 s) is what Ctrl-C costs when the network has just gone away.
        close_timeout=2,
    ) as ws:
        log.info("WSS connected; spawning subprocess: %s", " ".join(cmd))
        # asyncio's default StreamReader buffer is 64 KiB; a single MCP
        # response can easily exceed that (e.g. a chrome_take_snapshot on
        # a page like bankofamerica.com). When that happens the readline
        # loop dies with "Separator is not found, and chunk exceed the
        # limit" and the bridge kills the subprocess. Bump to 16 MiB.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env if env is not None else os.environ.copy(),
            limit=16 * 1024 * 1024,
        )
        tasks = {
            asyncio.create_task(_pump_stdout_to_ws(proc, ws), name="stdout->ws"),
            asyncio.create_task(_pump_ws_to_stdin(ws, proc), name="ws->stdin"),
            asyncio.create_task(_pump_stderr(proc), name="stderr->log"),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled() and task.exception():
                    log.warning("pump task %s exited with %s", task.get_name(), task.exception())
        finally:
            # However we got here (a pump ended, Ctrl-C cancelled us), no
            # pump outlives this cycle: an orphan would die later of the
            # closed socket and print "Task exception was never retrieved".
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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


class AuthRejected(Exception):
    """The gateway refused the token (HTTP 401/403); see run(exit_on_auth_failure)."""

    def __init__(self, status: int) -> None:
        super().__init__(f"token rejected (HTTP {status})")
        self.status = status


async def run(
    wss_url: str,
    token: str,
    cmd: Sequence[str],
    env: dict[str, str] | None = None,
    initial_backoff: float = 1.0,
    max_backoff: float = 60.0,
    exit_on_auth_failure: bool = False,
) -> None:
    """Main loop: reconnect ruthlessly with exponential backoff.

    With ``exit_on_auth_failure``, an HTTP 401 or 403 on the upgrade raises
    AuthRejected instead of retrying: the token was refused (revoked,
    expired, for another gateway) and only a new one will help, which is
    the caller's job (the tinker.dog installer asks for approval again).
    """
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
            if exit_on_auth_failure and e.response.status_code in (401, 403):
                log.error("WSS rejected the token (HTTP %s); exiting", e.response.status_code)
                raise AuthRejected(e.response.status_code) from e
            # Auth failed or path wrong — terminal, but we still loop
            # in case the gateway is briefly misconfigured. Long sleep.
            log.error(
                "WSS rejected (HTTP %s); will retry in %ds",
                e.response.status_code, max_backoff,
            )
            await asyncio.sleep(max_backoff)
        except ssl.SSLCertVerificationError as e:
            # Not transient: retrying won't fix a trust problem. Say what to
            # check, then keep trying (the user may fix it underneath us).
            log.error("TLS certificate check failed for %s: %s. This machine doesn't "
                      "trust the gateway's certificate; is a proxy or VPN intercepting "
                      "HTTPS? Retrying in %.1fs", wss_url, e.verify_message or e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
        except Exception as e:
            log.warning("bridge cycle failed: %s; reconnecting in %.1fs",
                        type(e).__name__ + ": " + str(e), backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
