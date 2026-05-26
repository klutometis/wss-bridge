# wss-bridge

A generic WebSocket-to-stdio bridge for MCP servers. Runs alongside
any stdio MCP server on a device behind NAT, dials outbound to an MCP
gateway over WSS, and pumps JSON-RPC frames between the WebSocket and
the subprocess's stdin/stdout.

**The bridge itself is MCP-unaware.** It just shovels newline-delimited
JSON. The same binary wraps any stdio MCP server you point it at via
`--cmd`.

## Architecture

See [`plans/mcp-over-wss.md`](https://github.com/klutometis/mcp-gateway/blob/main/plans/mcp-over-wss.md)
in the mcp-gateway repo for the design rationale, and
[`plans/multi-instance-backends.md`](https://github.com/klutometis/mcp-gateway/blob/main/plans/multi-instance-backends.md)
for how multi-host setups compose with the bridge.

```
device (NAT'd)                                      gateway
┌──────────────────────────┐                  ┌─────────────────────┐
│  stdio MCP subprocess    │◀── pipes ──────▶│                     │
│  (e.g. tmux-mcp-rs,      │                  │   wss-shim          │
│   chrome-devtools-mcp,   │                  │   (single-slot      │
│   gmail-mcp, ...)        │                  │    Registry)        │
│         ▲                │                  │         │           │
│         │ wss-bridge     │                  │         │           │
│         └────────────────│── outbound WSS ▶│  /bridge endpoint   │
└──────────────────────────┘                  └─────────────────────┘
```

## Install

```bash
git clone git@github.com:klutometis/wss-bridge.git
cd wss-bridge
uv sync
```

Then `uv run wss-bridge --help` (or `~/prg/wss-bridge/.venv/bin/wss-bridge --help`).

## Usage

```bash
wss-bridge \
    --wss-url wss://your-gateway/devices/<class>-<host> \
    --token   "$YOUR_BEARER_TOKEN" \
    --cmd     /path/to/your-mcp-server [server-args...]
```

Or via a TOML config file (`--config /path/to/config.toml`):

```toml
wss_url = "wss://your-gateway/devices/foo"
token   = "..."
cmd     = ["/path/to/your-mcp-server", "--arg"]
env     = { FOO = "bar" }   # optional, merged with os.environ
```

## Helper-binary dependencies

The bridge itself only needs Python ≥ 3.11 and `websockets`. But the
subprocess you wrap (passed via `--cmd`) needs its own runtime
installed on the device. This dep doesn't belong in wss-bridge's
`pyproject.toml` — the bridge is generic, not tied to any specific
MCP server — but it does need to be installed on the device.

The host-level install is handled by Peter's
[`~/bin/configure-system.sh`](https://github.com/klutometis/dotfiles/blob/main/bin/configure-system.sh)
in the "MCP helper binaries" section. Adding a new helper binary?
Add an install line there. **Avoid manual installs** — they create
snowflake machines that are painful to debug later.

Current helper binaries that gateway deployments expect:

| Binary                  | Runtime    | Install                           | Used by                       |
|-------------------------|------------|-----------------------------------|-------------------------------|
| `tmux-mcp-rs`           | Rust/cargo | `cargo install tmux-mcp-rs`       | `wss-bridge-tmux.service`     |
| `chrome-devtools-mcp`   | Node/bun   | `bun install -g chrome-devtools-mcp` | `wss-bridge-chrome.service` |

To onboard a new device as a helper:

1. Clone dotfiles, run `~/bin/configure-system.sh` (installs all
   helper binaries idempotently).
2. Clone this repo to `~/prg/wss-bridge` and `uv sync`.
3. Ensure the relevant MCP-server-side state is reachable (e.g. for
   chrome: a `chrome-personal` instance listening on
   `--remote-debugging-port=9223`).
4. Add the host to `MCP_HOST_ROLES` in `~/etc/secrets/dot-env` and
   push.
5. Enable the systemd user units for the helpers you want:
   `systemctl --user enable --now wss-bridge-tmux.service`,
   `systemctl --user enable --now wss-bridge-chrome.service`.

## Example

The `examples/stdio_echo_mcp.py` file is a tiny in-process stdio MCP
server you can use to smoke-test bridge connectivity without spawning
a real device-side MCP.

## Lifecycle

- Spawns the subprocess on each successful WSS connect.
- Kills the subprocess (SIGTERM, then SIGKILL after 5s) on any
  disconnect.
- Exponential reconnect backoff (1s → 60s cap) on transient failures.
- On WSS auth rejection (HTTP 4xx), waits 60s before retrying.

State is not preserved across reconnects — MCP `initialize` runs
fresh on each new WSS. The gateway-side `wss-shim` handles that
handshake automatically.

## Subprocess stdout buffer

Default `asyncio.StreamReader` buffer is 64 KiB; bumped to 16 MiB in
`bridge.py` because a single MCP response (e.g. a Chrome DOM snapshot
on a large page) can easily exceed the default and crash the readline
loop.
