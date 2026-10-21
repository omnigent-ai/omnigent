# Shared preferences at startup

A connected machine refreshes preferences when a new CLI session starts. Its
host freezes a private snapshot for that session before starting the runner.
Later sync and reconnect do not rewrite that snapshot. This layer connects the
[explicit controls](shared-preferences-workflows.md) to configuration resolution;
native plugin overlays and browser/managed controls remain separate layers.

## Choose once, then follow

On an isolated server with the preference API enabled:

```sh
omnigent preferences --server URL connect
omnigent preferences --server URL import SOURCE.yaml > plan.json
# Review the portable values before applying.
omnigent preferences --server URL apply plan.json
```

An unused workspace requires an explicit import/edit or `initialize-empty`.
Connect never publishes existing files. New machines can discover an already
initialized workspace with `OMNIGENT_SHARED_PREFERENCES=1`; this rollout gate is
off by default. A configured connection opts that machine in without the gate.
The four portable groups remain model choices, harness, ordered arguments and
plugin toggles. Commands, binaries, provider credentials and installations stay local.

## Outage and recovery

Discovery and network refresh have a five-second budget. A transient failure uses
a complete, identity-bound cache and reports a warning. Authentication failures,
changed logins and mismatched namespaces fail visibly. A fresh machine can retry
or choose `omnigent --temporary-preferences COMMAND`; this uses local defaults
without publishing. Place the option before the command. Both server and host
must understand its launch-frame field; an older server cannot carry this intent.

Host registration does not await preference synchronization. Reconnect starts at
most one task, does nothing with an empty outbox, and makes at most four attempts
with jitter and five-second network budgets. Permanent failures retain edits for
inspection. No preference request is added to message handling.

Session handoffs use owner-only JSON files, bound to workspace/account and
installation. New host and CLI runner launches use disposable per-runner files;
the runner claims ownership before startup. Host maintenance (or standalone
runner startup) removes files only after proving their owner is dead in the same
boot and PID namespace. Live, unknown and foreign owners are retained. Startup
also recovers files left by a previous host exit. Saved session preferences in
the server remain authoritative and can regenerate a handoff on a fresh host.

Legacy unprepared host launches retain their frozen resume files and the 2,048
snapshot cap; these are never pruned automatically. Reusing a session keeps its
saved shared and machine values; project and session choices retain precedence.

## Local launcher handoff

A wrapper that prepares native tools before starting the CLI can run
`omnigent preferences --server URL prepare-launch`. It returns a versioned JSON
handoff containing the frozen launch and effective default harness. Credentials,
commands and installation settings are omitted. A disconnected, non-enrolled
machine returns a null launch without making a preference request; initial
adoption and recovery use the same rules as ordinary CLI startup.

Write that JSON to an unlinked temporary file, seek to offset zero, and inherit
its descriptor (at least 3) into the new CLI process through
`OMNIGENT_PREFERENCE_HANDOFF_FD`. The CLI closes the descriptor after consuming
it, verifies the server and selected login, and uses that snapshot without a
second refresh. Wrapper failures should close their descriptor. Do not include
this variable in runner passthrough; runners receive the ordinary session snapshot.
Use this only for a new session. Resume, fork, management commands and explicit
temporary-default launches must skip preparation. The wrapper still owns local
tool setup; a selected unavailable harness must remain visible as an error.

## Verification and remaining integration

Follow `shared-startup` in the [feature map](../feature-map/preferences.md).
The real CLI test uses a mock model; the host boundary test uses a real Python
child. Native multipart tests simulate HTTP and do not launch vendor tools.

Managed sandbox hosts have no user credentials. Their authenticated server
handoff, Isaac defaults/policy and exact runtime pins require Universe integration.
This layer does not establish vendor model/plugin application, availability
warnings in the browser, or complete rollout readiness. Keep activation disabled
until those paths and server lifecycle limits are validated.
