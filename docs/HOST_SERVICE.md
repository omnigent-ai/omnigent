# Run the Omnigent host as a user service

`omni host enable` installs the host under the current user's operating-system
service manager:

- macOS: `~/Library/LaunchAgents/ai.omnigent.host.plist`
- Linux: `${XDG_CONFIG_HOME:-~/.config}/systemd/user/omnigent-host.service`

The service starts immediately and is configured to start again with the user's
login session. It does not install a root-owned system daemon.

## Enable a host

Use the configured server, or local mode when no server is configured:

```bash
omni host enable
```

Select a remote server explicitly:

```bash
omni host enable --server https://omnigent.example.com
```

Force local mode even when the global config names a remote server:

```bash
omni host enable --server ""
```

Each user has **one persistent service target**. Running `host enable` for a
different target replaces the previous service definition and reports the old
and new targets. Unmanaged `omni host --background` processes may still use
multiple targets.

## Start, stop, and disable

| Command | Immediate effect | Autostart definition |
| --- | --- | --- |
| `omni start` or `omni host --background` | Starts the installed service when it owns the requested target | Preserved |
| `omni host stop` | Stops sessions and the selected host | Preserved |
| `omni server stop` | Stops the local host service and local server | Preserved |
| `omni stop` | Stops all current Omnigent processes, including the host service | Preserved |
| `omni host disable` | Stops the host service | Removed |
| `omni host enable` | Installs or refreshes and starts the service | Enabled |

A stopped but enabled service starts again with the next user login. Run
`omni host enable` to start it again immediately, or `omni host disable` when
you no longer want autostart.

## Inspect status and logs

```bash
omni host status
omni host status --json
```

The **User service** block is separate from daemon rows. It reports the
installed definition, configured target, manager state, PID, autostart state,
and log location. This distinction matters when a service is installed but
stopped after a permanent configuration or authentication failure.

On macOS, service-manager stdout and stderr go to:

```text
~/.omnigent/logs/host/service.log
```

The host also writes timestamped process logs under
`~/.omnigent/logs/host/` (or `$OMNIGENT_DATA_DIR/logs/host/`).

On Linux, inspect the user journal:

```bash
journalctl --user -u omnigent-host.service
journalctl --user -u omnigent-host.service --follow
```

## Environment and credentials

The generated definition is mode `0600`, but it does not contain model-provider
keys, Databricks bearer credentials, database credential URIs, telemetry-token
headers, or custom credential passthrough values. The long-lived host daemon
and generic runner keep only non-secret runtime selectors, paths, flags,
locale, and proxy configuration.

Configure provider credentials through Omnigent's provider and secret stores.
The selected runner resolves the named provider when it launches a harness;
the host daemon does not retain the resulting API key. A credential available
only as an exported shell variable is intentionally not copied into the user
service or generic runner environment.

`host status` reports when the configured Python executable no longer exists;
rerun `host enable` from a working Omnigent installation to repair it.

## Upgrades

`omni upgrade` stops a local host service before replacing the installation.
After a successful upgrade it refreshes and restarts that service. If the
installer fails, Omnigent attempts to restore the previously running service.

A remote service is not interrupted automatically because it may own active
remote sessions. After a successful upgrade, run `omni host enable` when you
are ready to restart it on the new installation.

## Login and logout behavior

These are user services, not machine-wide daemons:

- **macOS:** a LaunchAgent belongs to the graphical user login session. It is
  loaded when that user logs in and is not an always-on pre-login daemon.
- **Linux:** a systemd user service normally follows the user's systemd manager.
  On distributions that stop the user manager at logout, the host also stops.

To intentionally keep Linux user services running after logout, an
administrator—or the user when local policy allows it—can enable lingering:

```bash
loginctl enable-linger "$USER"
```

Undo it with:

```bash
loginctl disable-linger "$USER"
```

Omnigent never changes lingering automatically because it is a machine login
policy, not merely a host-process setting.

## Troubleshooting

### Service is installed but stopped or failed

```bash
omni host status
omni host enable
```

If enable still fails, inspect the macOS service log or Linux user journal
shown above. Permanent authentication/configuration failures intentionally stop
instead of entering a restart loop.

### `host status` reports a stale executable

Upgrade or reinstall Omnigent, then regenerate the service definition:

```bash
omni host enable
```

### Stop appears temporary

`host stop` deliberately preserves autostart. Remove the service when the host
must stay off across future logins:

```bash
omni host disable
```

### Remove every Omnigent artifact

`omni uninstall` reads the install ledger and unloads/removes the recorded
launchd or systemd user service before removing the CLI.
