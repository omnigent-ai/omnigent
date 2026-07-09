# Keeping the local server running (launchd / systemd)

The cloud recipes in [`deploy/`](../README.md) give you a hosted server.
This page is for the other case: the **local** server on your own
workstation — the one the desktop app, `omnigent run`, and the host
daemon all talk to on `127.0.0.1:6767`. Out of the box it is started on
demand and detached; if it crashes, or you log out and back in, nothing
brings it back until something next asks for it. A user-level service
manager (launchd on macOS, systemd on Linux) fixes that: start at login,
restart on failure.

## The two rules

**1. Supervise the foreground command, not `server start`.**
`omnigent server start` is a daemonizer: it spawns the real server
detached and exits `0`. A supervisor pointed at it sees a process that
exits immediately and has nothing to keep alive. Bare `omnigent server`
runs in the foreground — that is the invocation a supervisor must own.

**2. Don't pass `--port` / `--database-uri` / `--artifact-location`.**
With no explicit flags the foreground server runs as the *canonical*
local server: it registers itself in `~/.omnigent/local_server.pid`, and
`omnigent run`, the host daemon, and the desktop app discover and reuse
it through that pidfile. Passing any of those flags makes it a
*dedicated* server that deliberately does **not** register — the rest of
the tooling won't find it and will spawn a competing second server.
The defaults are already what you want (`127.0.0.1:6767`, shared
`chat.db` and artifacts under the data dir).

## macOS: launchd

Save as `~/Library/LaunchAgents/ai.omnigent.server.plist`, replacing
`YOU` with your username (find your `omnigent` path with
`which omnigent`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.omnigent.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/omnigent</string>
        <string>server</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>/Users/YOU</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>/Users/YOU</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/YOU/.omnigent/logs/launchd-server.out.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOU/.omnigent/logs/launchd-server.err.log</string>
    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
```

Then:

```sh
# Stop any detached server first so the supervised one can bind 6767
omnigent server stop

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.omnigent.server.plist

# Verify
launchctl list | grep ai.omnigent.server   # shows a PID
omnigent server status
```

Day-to-day:

```sh
launchctl kickstart -k gui/$(id -u)/ai.omnigent.server   # restart (e.g. after upgrade)
launchctl bootout gui/$(id -u)/ai.omnigent.server        # stop for real (unload)
```

Note that `omnigent server stop` (and crashes, and plain `kill`) no
longer stop the server for long — `KeepAlive` restarts it. To actually
take it down, unload the job with `bootout`.

## Linux: systemd user unit

Save as `~/.config/systemd/user/omnigent-server.service`:

```ini
[Unit]
Description=Omnigent local server
After=network.target

[Service]
ExecStart=%h/.local/bin/omnigent server
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

Then:

```sh
omnigent server stop        # stop any detached server first
systemctl --user daemon-reload
systemctl --user enable --now omnigent-server
systemctl --user status omnigent-server
```

By default user units only run while you're logged in. To keep the
server up across logouts (headless boxes, SSH targets):

```sh
sudo loginctl enable-linger "$USER"
```

## Crash-restart port fallback

If the server is restarted within a second or two of a crash, the OS may
not have released port 6767 yet, and the respawn logs
`port 6767 is busy — using <other> instead.` and binds a free port. This
is safe: discovery goes through the pidfile, not the port, so
`omnigent run`, the daemon, and the desktop app still find it. Anything
of yours that hardcodes `http://127.0.0.1:6767` won't, though — check
`omnigent server status` for the live URL, and restart the service once
the old socket is gone to get 6767 back. The 10-second restart delay in
both recipes above makes this rare in practice.
