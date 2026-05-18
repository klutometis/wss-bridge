"""CLI entry: parse args/config, run the bridge.

Usage:
    wss-bridge --wss-url URL --token TOKEN --cmd CMD [args...]

Or with a TOML config file:
    wss-bridge --config /path/to/config.toml

Config file format:
    wss_url = "wss://mcp.example.com/devices/mac"
    token   = "..."
    cmd     = ["node", "/opt/imessage-mcp/dist/server.js"]
    env     = { GMAIL_KEY = "...", ... }     # optional, merged with os.environ
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tomllib
from pathlib import Path

from .bridge import run


def _load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="wss-bridge",
        description="WebSocket-to-stdio bridge for MCP servers behind NAT.",
    )
    parser.add_argument(
        "--config", type=Path,
        help="TOML config file (overridden by CLI flags / env vars).",
    )
    parser.add_argument(
        "--wss-url",
        help="WSS URL to dial. Or set WSS_BRIDGE_URL env var.",
    )
    parser.add_argument(
        "--token",
        help="Bearer token for WSS upgrade. Or set WSS_BRIDGE_TOKEN env var.",
    )
    parser.add_argument(
        "--cmd", nargs=argparse.REMAINDER,
        help="Subprocess command + args (must come last; everything after is the command).",
    )
    parser.add_argument(
        "--log-level", default=os.environ.get("WSS_BRIDGE_LOG_LEVEL", "INFO"),
        help="Logging level (default INFO).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    log = logging.getLogger("wss_bridge.cli")

    cfg: dict = {}
    if args.config:
        cfg = _load_config(args.config)

    # Precedence: CLI args > env vars > config file
    wss_url = args.wss_url or os.environ.get("WSS_BRIDGE_URL") or cfg.get("wss_url")
    token = args.token or os.environ.get("WSS_BRIDGE_TOKEN") or cfg.get("token")
    cmd = args.cmd if args.cmd else cfg.get("cmd")
    extra_env = cfg.get("env") or {}

    if not wss_url:
        parser.error("missing --wss-url (or WSS_BRIDGE_URL / config wss_url)")
    if not token:
        parser.error("missing --token (or WSS_BRIDGE_TOKEN / config token)")
    if not cmd:
        parser.error("missing --cmd (or config cmd=[...])")

    env = os.environ.copy()
    env.update({k: str(v) for k, v in extra_env.items()})

    log.info("starting bridge: wss=%s cmd=%s", wss_url, " ".join(cmd))
    try:
        asyncio.run(run(wss_url, token, cmd, env=env))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
