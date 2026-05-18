# macOS launchd unit for wss-bridge

Keeps a wss-bridge running 24/7 on a Mac, dialing into the
mcp-gateway sidecar over WSS so calls from the gateway can reach a
device-specific stdio MCP server (here: `imessage-mcp`).

## Files

- `com.example.wss-bridge.plist` — LaunchAgent definition. Drop in
  `~/Library/LaunchAgents/`. `RunAtLoad` + `KeepAlive` mean the
  bridge restarts on crash, sleep/wake, login.
- `wss-bridge-launcher` — wrapper that reads the bridge token from
  `~/.config/wss-bridge-mac.token`, sets `PATH`, and runs `caffeinate
  -dimsu` to keep the Mac awake (and the network up) while the
  bridge is alive. Drop in `~/bin/` (it's the `ProgramArguments`
  target referenced from the plist).

## Install

```bash
cp com.example.wss-bridge.plist  ~/Library/LaunchAgents/
mkdir -p ~/bin && cp wss-bridge-launcher ~/bin/ && chmod +x ~/bin/wss-bridge-launcher
launchctl load ~/Library/LaunchAgents/com.example.wss-bridge.plist
launchctl list | grep wss-bridge   # confirm
tail -f ~/Library/Logs/wss-bridge.log
```

## Full Disk Access

`imessage-mcp` reads `~/Library/Messages/chat.db`. macOS TCC denies
this from launchd-spawned processes by default, even though it works
from an interactive Terminal/SSH session (which inherits
Terminal.app's grant).

**Manual one-time step:** open System Settings → Privacy & Security
→ Full Disk Access, and add the imessage-mcp binary
(`~/prg/imessage-mcp/.build/debug/imessage-mcp`). Without this you'll
see `SQLite error 23: authorization denied` in the log.

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.example.wss-bridge.plist
rm ~/Library/LaunchAgents/com.example.wss-bridge.plist
```

## Debugging

- Logs: `~/Library/Logs/wss-bridge.log`
- Process: `launchctl list | grep wss-bridge` (column 1 = PID, 0 = not running)
- Manual run (skipping launchd) — same wrapper:
  `~/bin/wss-bridge-launcher`
