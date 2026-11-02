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

## The wrapper script (both platforms)

One wrinkle: when the supervisor replaces a running server, the new
process can start while the old one is still draining connections (or
its sockets sit in `TIME_WAIT`). The server's port probe then finds 6767
busy and silently binds a free port instead. Discovery still works —
everything finds the server through the pidfile, not the port — but
anything of yours that hardcodes `http://127.0.0.1:6767` breaks until
the next restart. In practice this happens on *most* restarts once the
desktop app or web UI holds long-lived WebSocket connections.

The fix is a short wait before the server starts. Save this as
`~/.omnigent/omnigent-server-wrapper.sh` and `chmod +x` it; both
recipes below run the server through it:

```sh
#!/bin/sh
# Wait (max 60s) for the previous server to release port 6767, then exec
# the foreground server so it reclaims its preferred port instead of
# falling back to a random one. The bind probe matches the one the
# server itself uses. exec keeps the server as the supervisor's direct
# child, so restart-on-failure still works.
python3 - <<'EOF'
import socket, time
for _ in range(60):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 6767))
        s.close()
        break
    except OSError:
        s.close()
        time.sleep(1)
EOF
exec "$HOME/.local/bin/omnigent" server
```

Adjust the `omnigent` path to `which omnigent` on your machine. Any
Python 3 works for the probe; omnigent installs one you can point at if
`python3` isn't on the service's PATH.

## macOS: launchd

Save as `~/Library/LaunchAgents/ai.omnigent.server.plist`, replacing
`YOU` with your username:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.omnigent.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>/Users/YOU/.omnigent/omnigent-server-wrapper.sh</string>
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

# Verify (first boot takes ~10-15s)
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
take it down, unload the job with `bootout`. On restart, expect a pause
while the wrapper waits for the previous instance to drain its
connections and release the port.

## Linux: systemd user unit

Save as `~/.config/systemd/user/omnigent-server.service`:

```ini
[Unit]
Description=Omnigent local server
After=network.target

[Service]
ExecStart=/bin/sh %h/.omnigent/omnigent-server-wrapper.sh
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

(`systemctl --user restart` waits for the old process to exit before
starting the new one, so the port race is narrower than on launchd —
but `TIME_WAIT` sockets from a crashed instance can still trigger the
fallback, and the wrapper costs nothing when the port is already free.)

By default user units only run while you're logged in. To keep the
server up across logouts (headless boxes, SSH targets):

```sh
sudo loginctl enable-linger "$USER"
```

## If you skip the wrapper

Running bare `omnigent server` as the supervised command still gives you
crash recovery and start-at-login — rules 1 and 2 are what matter for
correctness. You just lose the stable port: restarts that overlap the
old instance log `port 6767 is busy — using <other> instead.` and bind a
free port. That is safe by design (discovery goes through the pidfile;
check `omnigent server status` for the live URL), but hardcoded-6767
clients won't find the server until a later restart reclaims the port.
