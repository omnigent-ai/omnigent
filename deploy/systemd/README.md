# Omnigent on systemd

Run the Omnigent server as a long-running service on a Linux host you
already own — no Docker, no platform account. The server survives reboots,
restarts on failure, and logs to the journal.

This is the right target if you want Omnigent on a VPS or home server and
you'd rather manage a venv than a container. If you're happy with Docker,
[`../docker/`](../docker/README.md) is less work and gets you Postgres and
TLS in the same compose stack.

```
deploy/systemd/
├── omnigent-server.service       ← the unit template (edit 2 placeholders)
├── omnigent-server.env.example   ← EnvironmentFile template (paths + auth)
└── README.md                     ← this file
```

## What the unit runs, and what it must not run

`ExecStart` invokes the **foreground** server:

```
omni server --host 127.0.0.1 --port 6767 --no-open
```

Bare `omni server` runs uvicorn in-process: it blocks, streams logs to
stdout/stderr, and exits on SIGTERM. That is exactly the shape `Type=simple`
wants.

> [!WARNING]
> **Never use `omni server start` as `ExecStart`.** Despite the name, it is a
> detached *dev* daemon: it forks a background server and returns immediately.
> systemd would see the parent exit and tear the service down. It also
> hardcodes a loopback bind and sets `OMNIGENT_LOCAL_SINGLE_USER=1`, so it
> can't be configured for a real deploy. `start` / `stop` / `status` are for
> the local server that `omni run` and `omni claude` use — not for this.

Two flags are load-bearing:

- **`--no-open`** — the default is `--open`, which tries to launch a browser
  on the first boot of accounts auth. Headless, that's pointless.
- **`--port 6767`** — explicit, even though 6767 is already the default. A
  bare `omni server` with *every* default (loopback, default port, default
  db, default artifacts) is treated as the "canonical local server": if a
  healthy one is already running on the box — say a developer ran `omni run`,
  which spawns one — it prints `reusing it` and **exits 0**. systemd would
  mark the unit inactive and, with `Restart=on-failure`, never bring it back.
  Passing `--port` explicitly opts into the dedicated-server path, which
  always binds its own port. (`_is_canonical_local_server` in
  `omnigent/cli.py`.)

## Install

### 1. Install Omnigent somewhere the unit can reach

A systemd unit gets no login shell, so `$PATH` will not find a venv or pipx
binary — **`ExecStart` needs an absolute path**. Install to a system location
rather than a personal home directory:

```bash
sudo install -d -o omnigent -g omnigent /opt/omnigent
sudo -u omnigent python3 -m venv /opt/omnigent/venv
sudo -u omnigent /opt/omnigent/venv/bin/pip install omnigent
```

### Finding the `omni` binary

The template assumes `/opt/omnigent/venv/bin/omni`. If you installed
differently, resolve the real path and put *that* in `ExecStart`:

| Install method | Typical absolute path | How to find it |
|---|---|---|
| venv | `/opt/omnigent/venv/bin/omni` | `/opt/omnigent/venv/bin/omni --version` |
| pipx | `~/.local/bin/omni` (a symlink into `~/.local/share/pipx/venvs/omnigent/bin/`) | `pipx list --short`, or `readlink -f "$(which omni)"` |
| uv tool | `~/.local/bin/omni` | `uv tool dir`, or `readlink -f "$(which omni)"` |

```bash
readlink -f "$(which omni)"     # resolves symlinks to the real executable
```

`omni` and `omnigent` are the same entrypoint (`omnigent.cli:main`); either
works. Note that a pipx/uv install under `~/.local` lives in a home
directory — with `ProtectHome=yes` in the unit, the service cannot read it.
Either install to `/opt` as above, or drop `ProtectHome`.

### 2. Create the service account

```bash
sudo useradd --system --home-dir /var/lib/omnigent --shell /usr/sbin/nologin omnigent
```

The home directory matters: the server writes diagnostic logs to
`$HOME/.omnigent/logs/` and that path is **not** affected by
`OMNIGENT_DATA_DIR` (`state_dir()` is hardcoded to `Path.home()/".omnigent"`).
The unit pins `HOME=/var/lib/omnigent` so this is deterministic, and
`StateDirectory=omnigent` creates `/var/lib/omnigent` with the right owner
and makes it writable under `ProtectSystem=strict`.

### 3. Install the env file

```bash
sudo install -d -m 0750 -o root -g omnigent /etc/omnigent
sudo install -m 0640 -o root -g omnigent \
     omnigent-server.env.example /etc/omnigent/omnigent-server.env
sudoedit /etc/omnigent/omnigent-server.env
```

It's referenced as `EnvironmentFile=-…` (note the `-`), so a missing file is
not an error — the loopback default works without it.

### 4. Install and start the unit

```bash
sudo cp omnigent-server.service /etc/systemd/system/
sudoedit /etc/systemd/system/omnigent-server.service   # fix the EDIT-ME paths
sudo systemd-analyze verify /etc/systemd/system/omnigent-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now omnigent-server
```

## Operate

```bash
systemctl status omnigent-server           # is it up?
journalctl -u omnigent-server -f           # follow logs
journalctl -u omnigent-server -b --no-pager   # this boot
journalctl -u omnigent-server -p err        # errors only

sudo systemctl restart omnigent-server
sudo systemctl stop omnigent-server
sudo systemctl daemon-reload && sudo systemctl restart omnigent-server   # after editing the unit
```

`journalctl` is the source of truth: `StandardOutput`/`StandardError` are
both `journal`. The server *also* writes a per-invocation diagnostic log
under `/var/lib/omnigent/.omnigent/logs/` — useful when a crash happens
before uvicorn's logging is up.

Confirm it's actually serving:

```bash
curl -fsS http://127.0.0.1:6767/ >/dev/null && echo ok
```

## Uninstall

```bash
sudo systemctl disable --now omnigent-server
sudo rm /etc/systemd/system/omnigent-server.service
sudo systemctl daemon-reload
sudo rm -rf /etc/omnigent          # config + secrets
sudo rm -rf /opt/omnigent          # the venv
sudo rm -rf /var/lib/omnigent      # DATA: db, artifacts, logs. Deletes conversations.
sudo userdel omnigent
```

## Exposing beyond loopback

> [!CAUTION]
> **Changing `--host` is not enough, and getting this wrong gives you a
> server that either rejects everything or trusts everyone.**
>
> The default `--host 127.0.0.1` is the only bind that works with no auth
> configuration. On loopback the server marks itself single-user (identity
> `local`, no login) — safe, because only processes on the box can reach it.
>
> A **non-loopback bind does not get that fallback**. The server still starts
> — there is no startup check that stops you — but auth resolves to `header`
> mode, and every request arriving without an `X-Forwarded-Email` header is
> rejected with **401**. So an exposed server with no auth set up isn't
> wide open; it's simply unusable. You must configure auth.

To expose it, do **both**:

**1. Set the bind and the auth env.** In the unit:

```ini
ExecStart=/opt/omnigent/venv/bin/omni server --host 0.0.0.0 --port 6767 --no-open
```

In `/etc/omnigent/omnigent-server.env`, pick one mode:

```dotenv
# Built-in accounts — username/password, no external IdP. The simplest.
OMNIGENT_AUTH_ENABLED=1
OMNIGENT_ACCOUNTS_COOKIE_SECRET=<openssl rand -hex 32>
OMNIGENT_ACCOUNTS_BASE_URL=https://omnigent.example.com   # the URL the BROWSER sees
OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD=<first-run admin password>
```

```dotenv
# OIDC — your own IdP. Setting the issuer is what selects this mode.
OMNIGENT_AUTH_ENABLED=1
OMNIGENT_DOMAIN=omnigent.example.com
OMNIGENT_OIDC_ISSUER=https://accounts.google.com
OMNIGENT_OIDC_CLIENT_ID=...
OMNIGENT_OIDC_CLIENT_SECRET=...
OMNIGENT_OIDC_COOKIE_SECRET=<openssl rand -hex 32>
```

Set `OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD` for accounts mode: the unit is
headless, so there's no TTY for the first-run admin prompt. (The web
Create-admin form is the other route.)

Header mode (`OMNIGENT_AUTH_PROVIDER=header`) is for deploys already behind
a trusted SSO proxy. If that proxy doesn't **strip** a client-supplied
`X-Forwarded-Email`, anyone can impersonate anyone. Read
[`../docker/README.md#header-proxy-mode-for-deploys-behind-an-existing-sso-proxy`](../docker/README.md)
before choosing it.

**2. Front it with TLS.** Both accounts and OIDC modes issue session cookies;
OIDC *requires* HTTPS (the cookie uses the `__Host-` prefix). Don't put a
plaintext `0.0.0.0:6767` on the public internet.

The repo already ships a Caddy overlay that terminates TLS and gets certs
automatically — [`../docker/Caddyfile`](../docker/Caddyfile) and
[`../docker/docker-compose.https.yaml`](../docker/docker-compose.https.yaml).
The cleanest split is to keep the server on loopback under systemd and let a
reverse proxy own :443:

```ini
# unit stays on the safe default
ExecStart=/opt/omnigent/venv/bin/omni server --host 127.0.0.1 --port 6767 --no-open
```

…with Caddy (or nginx) proxying `omnigent.example.com` → `127.0.0.1:6767`.
Note that even on a loopback bind, once a proxy fronts it you are serving
real users: set `OMNIGENT_AUTH_ENABLED=1` and the accounts/OIDC vars anyway.
The loopback single-user fallback means the server itself won't ask for a
login, so **without auth enabled, anyone who reaches the proxy is user
`local`** — full access to every conversation.

If you only need to share a local server briefly, a Cloudflare quick tunnel
avoids all of this: `cloudflared tunnel --url http://localhost:6767`.

## Run as a user service instead

For a single-user box (your own laptop or dev VM), a `--user` unit avoids
root, the dedicated account, and the absolute-path juggling — it inherits
your `$PATH`, `$HOME`, and your existing `~/.omnigent` data.

```bash
mkdir -p ~/.config/systemd/user
cp omnigent-server.service ~/.config/systemd/user/
```

Then edit it down:

- Delete `User=`, `Group=`, `StateDirectory=`, and `Environment=HOME=…`
  (a user unit already runs as you, with your home).
- Delete `ProtectHome=yes` — it would hide the home directory the server
  needs.
- Change `WantedBy=multi-user.target` to `WantedBy=default.target`.
- Keep `ExecStart` absolute anyway (`readlink -f "$(which omni)"`).

```bash
systemctl --user daemon-reload
systemctl --user enable --now omnigent-server
journalctl --user -u omnigent-server -f
```

By default a user unit stops when you log out. To keep it running:

```bash
sudo loginctl enable-linger "$USER"
```

## Hardening notes

The unit ships the hardening that was checked against what the server
actually does. Two directives are deliberately **absent**:

- **`MemoryDenyWriteExecute`** — the server owns a `HarnessProcessManager`
  and can spawn runner subprocesses, which exec the Node-based coding CLIs.
  W^X breaks their JIT.
- **`PrivateNetwork` / `IPAddressDeny`** — the server must reach LLM APIs and
  accept runner tunnel connections.

`ProtectHome=yes` is included but is the most likely one to bite you. It
masks `/home`, `/root`, and `/run/user`. That's fine for the default layout
(the service account's home is `/var/lib/omnigent`), but drop it if the
account's home is under `/home`, if you installed `omni` via pipx/uv under
`~/.local`, or if agents need to work on repos under `/home`.

If the service fails to start after you tighten something, `systemd-analyze
verify` catches syntax errors, and `journalctl -u omnigent-server -p err`
shows what the sandbox denied.
