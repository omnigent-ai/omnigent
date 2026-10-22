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
installation. Their directory admits at most 2,048 snapshots and fails visibly
when full; discard files only for sessions you no longer intend to resume. It
never evicts an active session to make room. Reusing a session keeps its frozen
shared and machine values; project and session choices retain their precedence.

## Verification and remaining integration

Follow `shared-startup` in the [feature map](../feature-map/preferences.md).
The real CLI test uses a mock model; the host boundary test uses a real Python
child. Native multipart tests simulate HTTP and do not launch vendor tools.

Managed sandbox hosts have no user credentials. Their authenticated server
handoff, Isaac defaults/policy and exact runtime pins require Universe integration.
This layer does not establish vendor model/plugin application, availability
warnings in the browser, or complete rollout readiness. Keep activation disabled
until those paths and server lifecycle limits are validated.
