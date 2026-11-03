"""TLS to the gateway uses the OS trust store (bridge._tls_context).

Reproduces Krish's 2026-11-02 failure: an interpreter whose certificate
defaults point nowhere (python.org Python before "Install Certificates", or
SSL_CERT_FILE left at a bad bundle) failed with "unable to get local issuer
certificate". With the OS trust store the TLS handshake succeeds; proof is the
gateway's own answer (403 to a bogus token -> --exit-on-auth-failure -> 77).

Needs network: dials the live wss://mcp.tinker.dog/devices/chrome with a
bogus token (it is refused; nothing connects). Skipped offline.
"""

import os
import socket
import subprocess
import sys

import pytest

GATEWAY = "wss://mcp.tinker.dog/devices/chrome"


def _online() -> bool:
    try:
        socket.create_connection(("mcp.tinker.dog", 443), timeout=5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _online(), reason="mcp.tinker.dog unreachable")
def test_tls_works_even_when_pythons_certificate_defaults_are_broken():
    env = {**os.environ, "SSL_CERT_FILE": "/nonexistent", "SSL_CERT_DIR": "/nonexistent"}
    p = subprocess.run(
        [sys.executable, "-m", "wss_bridge", "--wss-url", GATEWAY, "--token", "bogus",
         "--exit-on-auth-failure", "--cmd", sys.executable, "-c", "import time; time.sleep(30)"],
        env=env, capture_output=True, text=True, timeout=60)
    assert p.returncode == 77, p.stderr[-800:]          # the gateway answered: TLS succeeded
    assert "CERTIFICATE_VERIFY_FAILED" not in p.stderr


def test_plain_ws_needs_no_tls_context():
    from wss_bridge.bridge import _tls_context
    assert _tls_context("ws://127.0.0.1:9/bridge") is None
    assert _tls_context("wss://example.com/bridge") is not None
