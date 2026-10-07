# Deliberate host and session shutdowns

Stopping a host or session can close the same runner transport that a crash
closes. The server records the requested action before teardown and uses it
only for that runner connection and session lifecycle. A matching disconnect
settles to `idle`; a preceding failure and its error labels stay intact.

## Evidence and ordering

| Action | Reason | Initiator |
| --- | --- | --- |
| Session Stop or archive | `user_stopped_session` | Authenticated request user when available |
| `omnigent host stop`, including `--force` and `--daemon-only` | `user_stopped_host` | `local_cli`; user ID only when verified by the HTTP endpoint |
| `omnigent host disable` | `user_stopped_host` | Same CLI provenance; only the service manager's current host PID is targeted |
| Foreground Ctrl+C / observed SIGINT | `host_interrupted_sigint` | `unknown`, with no user ID |
| Raw SIGTERM or SIGHUP | `unknown` | `unknown`; signal name retained |
| Unobserved SIGKILL, power loss, or crash | No requested-shutdown evidence | Unknown |

The SIGINT category is **Host stopped by interrupt (SIGINT)**. Seeing SIGINT
does not identify its sender. Host ownership also does not identify the person
who ran a local CLI command. Body-supplied user IDs are never trusted.

The host installs signal handlers before its main loop. Runners and the POSIX
zygote use separate process groups so terminal Ctrl+C reaches the host before
its children terminate. Direct Windows runners use `CREATE_NEW_PROCESS_GROUP`;
native Windows console delivery still needs platform verification. A second
interrupt escapes the notification wait or synchronous runner cleanup.
On POSIX, TERM/HUP keep the startup crash-reporting handler's three-second
watchdog and signal exit status. Notification runs inside that deadline;
inherited ignored signals and native signal dispositions remain untouched.

Host notification and acknowledgment have a two-second budget. CLI notification
also has a two-second budget, including credential discovery, and does not
depend on listing sessions. The local mailbox is addressed to an exact host
process UUID and PID. Failed notification, mailbox cleanup, or service
instrumentation must not prevent termination. If evidence is unavailable, the
server retains the ordinary failure classification.
The server records host evidence on one task per connection, so persistence
does not block heartbeat or control replies. It acknowledges after recording,
cancels the task on disconnect, and rechecks host identity during storage reads.

```mermaid
sequenceDiagram
    participant Caller as User / host signal handler
    participant Host as Host connection
    participant Store as Shared session metadata
    participant Runner as Runner connection replica
    Caller->>Host: Stop intent with shutdown ID and host incarnation
    Host->>Store: Record affected runner + connection + lifecycle
    Store-->>Host: Attempt completed
    Host->>Runner: Tear down runners
    Runner->>Store: Compare current scope and exact intent
    Store-->>Runner: Set idle only if still matching; preserve failed status
```

`host_shutdown_requested` reports host identity, process UUID, connection UUID,
PID, action, reason, initiator, original `requested_at_ms`, and `shutdown_id`.
The server intersects reported runner IDs with the authenticated connection's
inventory and checks each conversation's host, including inherited child host
bindings.

`session_shutdown_requested` adds `session_id`, `runner_id`,
`runner_connection_id`, `lifecycle_id`, optional `response_id`, `eligible`, and
`shutdown_recorded_at_ms`. Only `eligible=true` means a scoped intent was
persisted. The server observation time governs ordering; the original client
timestamp is preserved, with up to 30 seconds of clock skew permitted for
acceptance.

`session_shutdown_applied` carries the same identity and ordering fields and
`preserve_failure`. A successfully attributed `runner_stream_disconnected`
outcome also carries those fields, `decision=intentional_stop`,
`intentional_stop=true`, and `lost_at_ms`. A host event or an intent event alone
does not prove that a specific error was caused by the shutdown.

The private metadata is stored with live status, using the existing metadata
store and a row-locked compare-and-set. It is hidden from policy state and is
not copied to a fork. New turns and runner connections replace the scope and
clear active intent. A bounded replay ledger retains observed shutdown IDs
across those transitions, so a delayed HTTP/frame duplicate cannot arm the old
command against a fresh lifecycle. Conditional live-status writes and dedupe
invalidation prevent old writes or settlement from overwriting a new turn.
The metadata payload is capped at 8 KiB; replay history is bounded to 32 entries
and the acceptance horizon. Saturation declines attribution.

Runner discovery uses pages of 200 session IDs and statuses through the existing
workspace/runner/session index. Partial pages and live relays remain available
if a later page fails. Each metadata read or conditional write addresses one
workspace/session key. A cold active child inherits its runner's known
connection before intent is recorded.

Stop attempts are serialized per runner on each replica. A dispatched request
retains evidence when its acknowledgment is lost; an undispatched or definitively
rejected request restores any still-current earlier Stop. PTY running activity
within the stopped turn keeps its evidence. A completed turn followed by new
running activity, a new response, or a new user turn replaces the lifecycle.
Offline settlement retries transient metadata failures and rechecks binding
before publishing a result.

## Downstream KPI 3 classification

No maintained KPI 3 SQL or dashboard classifier was found in this repository.
**The live dashboard is unchanged.** Its owner must add the following rule to
the error normalization/classification stage.

Classify an individual error record, identified by `error_signal_log_id` (or the
original source log ID), as requested shutdown only when all these conditions
hold:

1. It is a transport-disconnect or process-termination observation directly
   associated with a successfully applied shutdown. A model/provider error,
   tool failure, startup failure, or independent crash report never qualifies.
2. The server's eligible intent, applied outcome, and error refer to the same
   workspace, session, runner, runner connection, lifecycle, and shutdown ID.
   For host actions, also require the same host ID, host process UUID, and host
   connection UUID. Reject missing or conflicting identities. If a runner-side
   row lacks the lifecycle/correlation fields, retain it unless its exact
   transport observation is independently linked to the server outcome; a
   session-and-time join is insufficient.
3. The reason/action pair is one of the requested combinations above. Require
   `requested=true`; raw TERM/HUP, clean exit, `local_shutdown`, parent-death
   messages, and host ownership do not establish a requested shutdown.
4. The server observed the intent before transport loss:
   `0 <= lost_at_ms - shutdown_recorded_at_ms <= 120000`. The affected error
   must occur at or after that loss and inside the same bounded shutdown
   episode. Use original observation time, not delayed ingestion time. A
   client timestamp alone cannot prove cross-process ordering.
5. No intervening `session_lifecycle_started` or different runner connection
   exists between intent and the affected observation. A later Stop cannot
   explain an earlier loss, even when its error log was delayed.
6. Keep all earlier or unrelated error rows. `preserve_failure=true` explicitly
   means the session already had a failure; it does not grant an exclusion to
   that failure.

Materialize a distinct set of attributable **error IDs**, then exclude only
those IDs from `window_eligible_errors`. For example, the relational operation
is an anti-join on `(workspace_id, error_signal_log_id)` against that set. Do
not anti-join on session ID, add a historical-stop session filter, or alter
`window_active` (the active-session denominator). Apply the same per-error rule
to detail rows and the numerator so drill-down totals agree.

Required downstream examples:

| Observations | Error eligibility | Denominator |
| --- | --- | --- |
| Session A model error, then Stop and attributable disconnect | Keep model error; exclude only attributable disconnect | A remains active |
| Session B Stop, reconnect/new turn, then unexpected disconnect | Keep new-turn disconnect | B remains active |
| Session C SIGINT, unknown actor, matching applied disconnect | Exclude that disconnect as **Host stopped by interrupt (SIGINT)** | C remains active |
| Session D raw TERM/HUP or parent-death message | Keep error absent matching explicit-command evidence | D remains active |
| Session E crashes, then Stop arrives during disconnect grace | Keep original crash/disconnect | E remains active |

## Verification

These tests use local stores, fake peers, and disposable child processes. They
do not require provider credentials, a running personal host, or a production
server:

```sh
.venv/bin/python -m pytest tests/server/test_shutdown_attribution.py \
  tests/host/test_shutdown_attribution.py tests/host/test_shutdown_signals.py \
  tests/host/test_crash_reporting.py tests/server/integration/test_shutdown_routes.py \
  tests/server/routes/test_sessions_runner_relay.py \
  tests/server/integration/test_session_host_launch.py -q
```

The signal tests send signals only to process groups they created. They check
direct and zygote runners, repeated Ctrl+C, unavailable acknowledgment, a
runner that died before the signal, unknown TERM/HUP, ignored SIGHUP, a blocked
event loop, and SIGKILL. Native parent/child checks save original structured
shutdown events separately from final state: a child completion notice can
start a new parent runner, whose scope must have no active old shutdown intent.

### Manual check on Linux or macOS

Use Bash terminals in this checkout. Prepare the development dependencies and
UI once, following `CONTRIBUTING.md`:

```sh
uv sync --extra all --group dev
pnpm install --frozen-lockfile --filter web
pnpm --dir web run build
```

Create an isolated runtime. Ports **16767** (server) and **16768** (mock model)
must be free; if either is occupied, choose unused ports and replace them in
the commands below. Every CLI invocation uses an empty environment with its
own HOME, config, data, and caches. The model key below is a dummy value used
only by the local mock.

```bash
shutdown_check="$(mktemp -d /tmp/omnigent-shutdown-check.XXXXXX)"
mkdir -p "$shutdown_check"/{home,config,data,cache,workspace,artifacts}
git rev-parse --show-toplevel > "$shutdown_check/repo"
cat > "$shutdown_check/env.sh" <<'SH'
shutdown_check="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
shutdown_repo="$(cat "$shutdown_check/repo")"
shutdown_url=http://127.0.0.1:16767
shutdown_mock=http://127.0.0.1:16768
shutdown_run() (
  cd "$shutdown_repo" || return
  env -i PATH="$shutdown_repo/.venv/bin:/usr/bin:/bin" TERM="${TERM:-xterm}" \
    HOME="$shutdown_check/home" \
    XDG_CONFIG_HOME="$shutdown_check/config" \
    XDG_DATA_HOME="$shutdown_check/data" XDG_CACHE_HOME="$shutdown_check/cache" \
    OMNIGENT_CONFIG_HOME="$shutdown_check/config" \
    OMNIGENT_DATA_DIR="$shutdown_check/data" \
    OMNIGENT_AUTH_PROVIDER=header OMNIGENT_LOCAL_SINGLE_USER=1 \
    OMNIGENT_SKIP_ONBOARD=1 OMNIGENT_NO_UPDATE_CHECK=1 \
    OMNIGENT_DISABLE_CATALOG_LOOKUP=1 OMNIGENT_SSE_LOG_TO_FILE=1 \
    OPENAI_API_KEY=mock-key OPENAI_BASE_URL="$shutdown_mock/v1" \
    PYTHONPATH="$shutdown_repo" "$@"
)
SH
cat > "$shutdown_check/config/config.yaml" <<'YAML'
auth:
  type: api_key
YAML
cat > "$shutdown_check/agent.yaml" <<'YAML'
name: shutdown-check
prompt: This is a disposable shutdown verification session.
executor:
  harness: openai-agents
  model: mock-shutdown
tools:
  sleep:
    type: function
    description: Sleep for a requested number of seconds.
    callable: tests.resources.examples._shared.tool_functions.sleep_tool
YAML
source "$shutdown_check/env.sh"
printf 'Run this in each terminal: source %q/env.sh\n' "$shutdown_check"
printf 'Disposable workspace: %s/workspace\n' "$shutdown_check"
```

In each of four terminals, run the exact `source .../env.sh` command just
printed. Start the mock model in terminal 1 and leave it running:

```sh
shutdown_run python -m tests.server.integration.mock_llm_server 16768 \
  > "$shutdown_check/mock.log" 2>&1
```

Start the server in terminal 2 and leave it running. This explicitly selects
the disposable SQLite database and artifact directory:

```sh
shutdown_run python -m omnigent server --host 127.0.0.1 --port 16767 \
  --database-uri "sqlite:///$shutdown_check/chat.db" \
  --artifact-location "$shutdown_check/artifacts" \
  --agent "$shutdown_check/agent.yaml" --no-open \
  > "$shutdown_check/server.log" 2>&1
```

In terminal 4, wait until both checks succeed:

```sh
curl --fail --noproxy '*' "$shutdown_mock/stats"
curl --fail --noproxy '*' "$shutdown_url/health"
```

Start the foreground host in terminal 3. Wait for **Listening for sessions**:

```sh
shutdown_run python -m omnigent host --server "$shutdown_url" \
  --no-open --non-interactive
```

After the previous turn has stopped and before **each** new turn below, reset
and arm the mock in terminal 4. Reset clears old canceled gates; the tool guard
prevents background title generation from consuming the blocked response:

```sh
curl --fail --noproxy '*' -X POST "$shutdown_mock/mock/reset"
curl --fail --noproxy '*' "$shutdown_mock/mock/configure" \
  -H 'Content-Type: application/json' \
  --data '{"match":"shutdown-check","required_tools":["sleep"],"responses":[{"text":"done","block":true}]}'
```

Open **http://127.0.0.1:16767** in a browser. Create a new session with agent
**shutdown-check**, the connected local host, and the disposable workspace
path printed above. Send exactly **shutdown-check**. Confirm the turn is
running and the following command reports a pending gate before stopping it:

```sh
curl --fail --noproxy '*' "$shutdown_mock/gate/pending"
```

First open the session's **sidebar row menu → Stop session**. It should become
idle with no new failure banner. Arm the mock again and send **shutdown-check**
in the same session.
This time press **Ctrl+C in terminal 3**, leaving the server running. The host
should exit, the host badge should go offline, and the session should settle
to idle without a new runner-failure banner. The host/server log category is
exactly **Host stopped by interrupt (SIGINT)**; the signal initiator is unknown.

Copy the session ID from the browser's `/c/<session-id>` URL. Inspect both the
saved live status and scoped evidence in terminal 4:

```sh
shutdown_session=REPLACE_WITH_SESSION_ID
shutdown_run python - "$shutdown_check/chat.db" "$shutdown_session" <<'PY'
import json
import sys
from omnigent.server.shutdown_attribution import SessionShutdown
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

store = SqlAlchemyConversationStore("sqlite:///" + sys.argv[1])
state = store.get_shutdown_state(sys.argv[2])
assert state is not None
evidence = SessionShutdown.model_validate_json(state["intent"])
print(json.dumps({"session_id": sys.argv[2], "live_status": state["live_status"],
                  **evidence.log_attrs()}, indent=2))
PY
rg 'Host stopped by interrupt|Shutdown requested for session|Runner disconnect attributed' \
  "$shutdown_check/server.log" "$shutdown_check/data/logs"
```

For the Ctrl+C case expect `live_status=idle`,
`reason=host_interrupted_sigint`, `signal_name=SIGINT`, `initiator=unknown`,
`initiator_user_id=null`, and nonempty shutdown, host-process,
host-connection, runner-connection, and lifecycle IDs. A running-to-idle
update can take a few seconds after the host exits. The saved evidence is
cleared when the next turn or runner connection begins.

For each CLI stop variant, start a fresh background host with the command
below, create another disposable session, arm the mock, and send
**shutdown-check**. Run **one** stop command from the table after the gate is
pending, then repeat the setup for the next row:

```sh
shutdown_run python -m omnigent host --server "$shutdown_url" \
  --background --no-open --non-interactive
```

| Action | Command in terminal 4 | Expected evidence |
| --- | --- | --- |
| Normal host stop | `shutdown_run python -m omnigent host stop --server "$shutdown_url"` | `reason=user_stopped_host`, `action=host_stop` |
| Forced host stop | `shutdown_run python -m omnigent host stop --server "$shutdown_url" --force` | Same reason/action, `force=true` |
| Daemon-only stop | `shutdown_run python -m omnigent host stop --server "$shutdown_url" --daemon-only` | Same reason/action, `daemon_only=true` |

Each command should stop only this isolated target and leave its previously
running sessions idle without a new failure. Inspect the saved evidence using
the command above and the new session ID. The category is **Host stopped by
command**. The local CLI initiator remains `local_cli`; only the
authenticated HTTP path can add its verified local user ID.

To check preservation of a preceding error, start the host again and replace
the mock configuration with:

```sh
curl --fail --noproxy '*' -X POST "$shutdown_mock/mock/reset"
curl --fail --noproxy '*' "$shutdown_mock/mock/configure" \
  -H 'Content-Type: application/json' \
  --data '{"match":"shutdown-failure","required_tools":["sleep"],"responses":[{"error":"intentional mock failure","status_code":400}]}'
```

Create another session and send **shutdown-failure**. Wait for the model error
to appear, copy its session ID into `shutdown_session`, and run:

```sh
shutdown_run python -m omnigent host stop-session "$shutdown_session" \
  --server "$shutdown_url"
```

The existing failure and error labels must remain. For an ordinary stopped
session, reconnect and send another blocked turn: the saved lifecycle and
runner-connection IDs must change before a later disconnect is considered.
The automated lifecycle tests also verify that an unexpected disconnect in
that later turn remains a failure.

Service disable is tested with a mocked service manager:

```sh
.venv/bin/python -m pytest tests/host/test_shutdown_attribution.py -k disable -q
```

Run a real `shutdown_run python -m omnigent host disable` only in a disposable
VM where the test installed the service. It should target that service's PID,
emit `action=host_disable`, and leave any separate foreground host alone.
These instructions and process tests do not claim native Windows verification.

For cleanup, stop any remaining background host using the isolated
`host stop --server "$shutdown_url" --force` command above, then press Ctrl+C
in the foreground-host, server, and mock-model terminals that you started.
After they exit, the printed `omnigent-shutdown-check.*` directory can be
removed.
