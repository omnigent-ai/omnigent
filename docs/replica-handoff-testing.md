# Verify session recovery across server replicas

When NGINX routes a host to a new server, its tunnels and the browser may
reconnect at different times. These checks exercise interrupted message
sends, runner reconnection, and the browser state after recovery.

The [NGINX/Kubernetes example](../deploy/kubernetes/multi_replica/README.md)
explains the deployment and host-ID routing. Start that example before
running the Kubernetes recorder below. The individual regression tests
start their own server processes and do not require Kubernetes.

## Recovery behavior

- A `wrong_replica` response means that replica could not deliver the request.
  The browser waits and retries the same request with the same host ID.
  It starts retries for up to 15 seconds. A request already in progress keeps
  the caller's normal timeout or cancellation behavior; this is not an overall
  HTTP deadline.
- A lost HTTP response leaves delivery uncertain. A `503 runner_unavailable`
  can also mean that a prompt was saved just before its runner tunnel closed.
  The browser does not resend the message, because a runner without
  duplicate detection would run it twice. It waits up to 15 seconds for
  evidence of delivery: the message's consumed event, or the message in the
  session snapshot after the browser reconnects. Saved evidence settles the
  send without an error unless the server recorded the runner refusing it.
  With no evidence by the end of the wait, the browser shows the error and
  preserves the draft.
- An SDK runner can finish a turn while its server connection is down. It
  remembers which persisted message IDs it accepted, whether through a
  forward, its reconnect scan, or a recovery turn started from saved history,
  and answers any later delivery of the same message as already accepted.
  The reconnect scan queues missed messages and keeps the local conversation
  history. Otherwise a reply waiting to be saved can make an already completed
  turn look unfinished and cause it to run again.
  A missed message recovered after newer input has arrived runs after that
  newer input; this does not guarantee the original send order across a
  disconnect. The marker written by Stop does not start a new turn.
- The runner also sends its final SDK session status again after reconnecting.
  This clears a stale "Working…" state if the old server lost the completion.
  An actual failed turn keeps its error.
- When a new server misses the start of a reply, the browser still attaches
  the saved message ID to matching streamed text. Reading saved history on
  the next reconnect then keeps one copy of the reply.
- A terminal retries temporary routing misses with its host key. If a hidden
  terminal exhausts its retry budget, opening Terminal view starts a fresh
  attempt. Deliberately closed terminals stay closed.

## Record six browsers during a three-pod rollout

After starting the example, build the test host from the same server image
and increase the deployment to three replicas:

```bash
uv sync --frozen --group test
uv run --no-sync playwright install chromium
docker build -t omnigent-prototype-client:local \
  -f deploy/kubernetes/multi_replica/Client.Dockerfile deploy/kubernetes/multi_replica
deploy/kubernetes/multi_replica/run.sh kubectl scale deployment/omnigent --replicas=3
deploy/kubernetes/multi_replica/run.sh kubectl rollout status deployment/omnigent
uv run --no-sync python deploy/kubernetes/multi_replica/verify_browser.py \
  --kubeconfig "${PROTOTYPE_STATE_DIR:-/tmp/omnigent-nginx-prototype}/kubeconfig" \
  --url "http://localhost:${PROTOTYPE_PORT:-18081}" \
  --replicas 3 --hosts 6 --output /tmp/omnigent-browser-rollout
```

Use a new output directory for each run. The script starts six external hosts,
with two initially routed to each pod, and opens a separate Chromium browser
context for every host. It types and sends ordinary chat messages continuously
while Kubernetes replaces all three pods, waits for the old pods to disappear,
then sends two follow-up turns on each host. Only model replies are mocked.

Each `host-N` directory contains `browser.webm`, a Playwright trace, screenshots,
request logs, model requests, and the saved conversation. `summary.json` reports
the result. A pass requires every expected prompt and reply to be saved and
rendered exactly once, one model request per turn, and an idle session at the
end. The script checks for extra replies, visible errors, duplicate text, and
failed browser actions. Expected stream disconnects and successfully retried
HTTP errors remain in the logs.

Play the six videos and check that each numbered prompt gets one matching reply
and that the composer stays usable. The script removes its temporary hosts and
mock model processes, and leaves the three server pods running.

## Reproduce individual failures

The automated regressions isolate each failure found in the rollout recordings.
They start two real servers sharing a temporary SQLite database and artifact
directory, a host, its SDK runner, and Chromium. A small proxy holds or closes
real connections at the point needed to reproduce a failure. It forwards the
server's actual responses; only the external model is scripted.

Each case runs through both the web client's send API and the ordinary browser
composer. The latter types and clicks Send. Neither test replaces the app's
send function, recovery code, or event handling.

| Case | Failure reproduced |
| --- | --- |
| `wrong_replica` | The new replica rejects a send before the host's tunnels arrive. |
| `lost_ack` | The server accepts the message, but the browser loses the entire HTTP response. |
| `lost_forward` | The server saves a message, then loses the runner tunnel and returns `503 runner_unavailable`. |
| `completed_turn` | The runner finishes locally while reconnect recovery reads history ending in the already-accepted prompt. |
| `lost_idle` | The reply is saved, but the final idle event is lost, leaving the next message queued. |
| `missing_header` | The new server misses the start of a reply; later history reconciliation renders its text twice. |
| `stale_host` | An open tab retains the old host after another client moves the session through the host-launch API. |
| `terminal_reveal` | A hidden terminal uses up its reconnect attempts before the user opens Terminal view. |
| `stopped_turn` | After Stop and a completed later turn, reconnect recovery mistakes the interruption marker for a new prompt. |
| `lost_runner_ack` | The runner accepts the prompt, but its response to the server is lost, and the browser reconnects only after the turn finishes. |

The terminal test advances the browser clock through the retry delays.

Install tmux and the test dependencies, then run from the repository root:

```bash
uv sync --frozen --extra all --group test
pnpm install --frozen-lockfile --filter web
uv run --no-sync playwright install chromium
OMNIGENT_E2E_REPLICA_HANDOFF=1 \
uv run --no-sync pytest \
  tests/e2e/test_replica_handoff_e2e.py \
  tests/e2e_ui/chat/test_replica_handoff.py \
  --ui-skip-build --video=on --screenshot=on --tracing=on \
  --output=/tmp/omnigent-handoff-browser -v
```

Use `-k lost_forward`, for example, to run one case in both suites. The
`Replica handoff regressions` CI workflow runs the complete set and uploads
videos, screenshots, traces, saved messages, model requests, and transport logs.
These tests check individual recovery paths; the three-pod, six-host command
above checks NGINX's actual endpoint reloads and Kubernetes rollout behavior.

## Coverage limits

The browser runs cover desktop Chromium and the OpenAI SDK harness with
scripted model replies. Native harnesses, mobile browsers, managed-host
startup, scheduled and background jobs, multiple ingress controllers, and
abrupt node loss need separate validation. Transient HTTP failures and
stream reconnects are expected; a passing run must recover without showing
errors in the active conversations.
