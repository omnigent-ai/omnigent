# Making Databricks credentials bullet-proof in Omnigent

## Summary

Omnigent currently allows several local processes to refresh the same Databricks credentials independently. When those refreshes overlap, Databricks can treat the shared refresh token as reused and invalidate the entire credential family. One collision can therefore break hosts, runners, forwarding, policy checks, Codex, and the web UI at once.

The fix is to make credential refresh a machine-wide shared service:

1. Detect permanently invalid credentials and report one clear error everywhere.
2. Allow only one process per machine and Databricks profile to refresh credentials.
3. Publish the resulting short-lived bearer token for the other local processes.
4. Recover automatically after the user logs in again.
5. Test the design under heavy concurrency and process crashes.

The plan is self-contained. It includes both the immediate host-to-runner token sharing needed to stop runner refresh collisions and the machine-wide coordination needed to stop collisions between separate hosts and other components.

## Goals

- **G1:** A machine performs at most one Databricks credential refresh at a time for each profile.
- **G2:** After the user logs in again, everything resumes automatically within about 20 seconds, without restarting sessions.
- **G3:** Every Omnigent component reports the same clear login remedy when credentials are permanently invalid.
- **G4:** A crashed refresher does not leave the machine stuck.
- **G5:** Normal requests are not made slower by this design.

## Non-goals

- Changing Databricks' refresh-token behavior.
- Synchronizing credentials across different machines.
- Making requests succeed while the Databricks login is genuinely invalid.
- Replacing Databricks CLI profiles with a new account system.

## The problem in plain English

Databricks gives a logged-in profile a long-lived refresh token. Omnigent uses it to obtain short-lived bearer tokens for requests.

Today, multiple Omnigent processes can decide to refresh at nearly the same time. They are all using the same refresh-token family. If two refreshes overlap, one process may use a token that another process has just replaced. Databricks can interpret that as unsafe reuse and invalidate the whole family.

After that, retries do not repair anything. They only produce many different-looking errors until the user runs:

```bash
databricks auth login --profile oss
```

The system should avoid the collision in the first place, recognize the permanently broken state if it occurs, and recover cleanly after login.

## Evidence and motivation

This is not only a theoretical race. An investigation of Corey Zumar's Omnigent host and runner logs covering August 19–22, 2026 found multiple episodes in which processes sharing the same Databricks profile attempted credential refresh around the same time and the machine subsequently lost working Databricks authentication.

The repeated incident pattern was:

1. Several Omnigent processes were active under the same operating-system user and Databricks profile.
2. More than one process entered a token-refresh path during the same period.
3. Databricks then rejected the shared refresh credentials as invalid.
4. The failure spread beyond the process that refreshed: hosts, runners, forwarding, and policy or model-facing requests began reporting different authentication-related symptoms.
5. Retrying did not repair the state. A new `databricks auth login` was required.
6. After login, normal operation returned until a later overlapping-refresh episode reproduced the problem.

The logs exposed the user impact through symptoms rather than one clean root-cause message. Depending on which component failed first, the visible result could look like a disconnected session, a runner that could not resume, failed event forwarding, a policy failure, or an unrelated model/service error. That made each incident slower to recognize and encouraged retries that could not succeed once the refresh-token family was invalid.

### What the evidence proves

The logs directly establish that multiple local processes shared one profile, entered refresh-related paths concurrently, and then experienced machine-wide authentication failure more than once during the August 19–22 window. The timing and Databricks' invalid-refresh response are consistent with refresh-token rotation and reuse detection.

The logs alone cannot prove Databricks' internal decision process or identify which individual refresh invalidated the family. The design therefore does not depend on that narrower claim. Independently of the exact server-side sequence, allowing several processes to mutate one credential source concurrently is unsafe. Serializing refresh, publishing one result, and giving permanent failure one shared state prevents both the observed race and equivalent variants of it.

### Why smaller fixes are insufficient

- Sharing a token only between one host and its runners reduces runner collisions but leaves separate hosts and other components racing each other.
- Adding retries cannot repair permanently invalid refresh credentials and increases noise.
- Adding a dead marker improves the error but does not prevent the original collision.
- Refreshing more aggressively can rotate credentials unnecessarily and increase the collision opportunity.

The machine-wide broker, shared token publication, and dead-state recovery must therefore be implemented together.

## Immediate host-to-runner protection

The first step is to stop runners from refreshing shared Databricks credentials.

The host obtains the bearer token and publishes it to a private local file. Each runner reads that file when it starts and rereads it after Databricks rejects its current bearer token. If the host is in the middle of publishing a replacement, the runner waits briefly for the new file rather than trying to refresh the Databricks profile itself.

This immediately removes the largest synchronized failure mode: many runners all deciding to refresh at once.

The initial implementation may use a token file belonging to one host process. The completed design replaces that with the shared profile-and-workspace file described below, allowing separate hosts and all other local components to coordinate too.

Required safeguards from the beginning:

- Publish at least once per minute and whenever a new token is obtained.
- Store JSON containing the token, expiry, profile, and workspace—not a bare token.
- Write atomically with permissions restricted to the current user.
- Log publication failures at `WARNING`; a silent failure can strand every runner.
- Validate ownership, permissions, profile, workspace, and expiry before using the file.
- Never let a runner invoke Databricks profile refresh, even as a fallback.

## Shared files and identity

Credential coordination must use one canonical location for the current operating-system user. It must not follow each session's configurable Omnigent data directory: two sessions with different data directories would otherwise acquire different locks and could refresh the same Databricks credentials concurrently.

Use a platform-specific, machine-local runtime directory for locks, with a private `0700` Omnigent subdirectory. Use a canonical per-user Omnigent state directory for token metadata and dead markers. A test-only override may replace these locations, but production processes must not choose them independently.

State is keyed by both Databricks profile and normalized workspace URL. The filename should contain a safe profile slug plus a stable hash of the normalized workspace URL; the full profile and URL remain inside the JSON for validation. This prevents one workspace from consuming or clearing another workspace's state when both use the same profile name.

Conceptually, the files are:

```text
<user-state-dir>/auth/databricks/<profile>-<workspace-hash>.json
<user-state-dir>/auth/databricks/<profile>-<workspace-hash>.dead
<machine-runtime-dir>/omnigent/auth/databricks/<profile>-<workspace-hash>.lock
```

The `databricks` directory prevents future credential providers from colliding with Databricks state. Workspace URLs must be normalized consistently—for example, lowercasing the hostname and removing a trailing slash—before hashing or comparison.

All directories and files must be owned by the current user. Secret-bearing files must use mode `0600`; private directories must use `0700`. Open files without following symbolic links, reject unexpected owners or permissive modes, cap file size, and reject malformed or mismatched JSON. Updates must use a temporary file in the same directory, flush it, and atomically replace the destination so readers see either the old complete file or the new complete file, never a half-written file.

The lock directory must be on a local filesystem with verified inter-process locking semantics. Omnigent must refuse to enable shared refresh if it cannot establish a trustworthy lock; it must not silently fall back to independent refreshes. Add platform tests for Linux and macOS and document which runtime-directory fallback is used when the preferred OS directory is unavailable.

## Layer 1: Recognize and contain dead credentials

### Strict error classification

Create one shared classifier for errors that definitively mean the login is no longer usable, such as:

- `invalid_grant`
- “refresh token is invalid”
- Other confirmed Databricks responses with the same permanent meaning

Timeouts, connection failures, server errors, and ambiguous messages must not create a dead marker. A bare HTTP `401` is also insufficient: it can mean an expired bearer token, the wrong workspace, or a permanently invalid login. Only an explicit permanent refresh-credential response from the trusted auth path may create the marker. Everything else remains an ordinary temporary or configuration failure.

### Dead marker

When a permanent error is confirmed, atomically write:

```text
<user-state-dir>/auth/databricks/<profile>-<workspace-hash>.dead
```

The marker should contain JSON with:

- profile name
- Databricks workspace URL
- detection time
- component that detected it
- sanitized error category and message
- exact recovery command

Do not store access or refresh tokens in the marker.

Once the marker exists, components stop repeatedly attempting expensive refreshes and show the same remedy:

```bash
databricks auth login --profile oss
```

### Component behavior

| Component | Behavior while the marker exists |
|---|---|
| Host | Stops expensive reconnect/refresh attempts. Checks the marker every 5 seconds. |
| Runner | Does not refresh independently. Waits and checks the marker every 5 seconds. |
| Forwarder | Pauses sending but retains its cursor so data is not skipped. |
| Policy hook | Continues to fail closed, but reports that Databricks login expired and gives the login command. |
| Codex integration | Reports an authentication problem instead of a misleading model or transport error. |
| Web UI | Shows that host authentication expired and displays the recovery command. |
| CLI | Fails quickly with the same clear explanation and command. |

### Clearing the marker

The marker is cleared only after Omnigent successfully obtains a real working token for the matching profile and workspace.

It is not cleared merely because:

- enough time passed,
- a process restarted,
- the marker is old, or
- the credential command started successfully but did not return a valid token.

At startup, a component may perform an immediate revalidation if a marker exists. This lets a restart recover promptly after the user has already logged in again.

A marker older than 24 hours should trigger an additional warning and diagnostic event. Age alone must not delete it.

### Recovery probe

While the credential is marked dead, one machine-wide probe is allowed every 15 seconds. The probe runs:

```bash
databricks auth token --profile oss
```

It does not explicitly request `--force-refresh` because the probe's job is only to answer: “Does the user's normal Databricks login work again?” After `databricks auth login`, the ordinary token command should return a usable token. Forcing a refresh would rotate credentials even when the current token is already valid, creating unnecessary writes and another opportunity for the same refresh-token collision this plan is designed to prevent.

The command returning a token is not enough by itself. Omnigent must verify that token with a small authentication-only request to the expected workspace. Before implementation, choose and test an endpoint that is available to every supported Databricks identity and does not require optional workspace permissions. Clear the marker only when that endpoint proves the token was accepted; a timeout, `403`, redirect, or ambiguous response is not sufficient. If no universal endpoint exists, add a dedicated server-side validation path rather than guessing from an unrelated API.

If the normal command keeps returning an unusable cached token after a successful login, invalidate only the known bearer-token cache entry under the shared lock and retry the ordinary command once. Do not force refresh repeatedly. If it still fails, preserve the marker and report a distinct “login succeeded but cached token remains unusable” diagnostic.

The probe is protected by the same profile-and-workspace lock used for refreshes. Before probing, the process checks the marker's modification time under the lock; if another process probed within 15 seconds, it does nothing.

On success it validates the returned token and workspace, publishes the token, and deletes the marker. Hosts and runners notice the deletion within 5 seconds and reconnect through their normal path, which usually takes about half a second.

Expected recovery time after login:

- Typical: about 10 seconds.
- Worst case: about 15 seconds until the probe, 5 seconds until a component notices, and roughly 1 second to reconnect—about 20 seconds total.

The marker prevents a false positive from lasting indefinitely. If a permanent-looking error was incorrectly classified, a successful probe repairs the state within roughly 15 seconds.

### Impact on user-perceived latency

There is no added latency during normal operation. Marker checks and recovery probes run only after a permanent credential failure has been detected; they are not on the normal request path.

While credentials are dead, requests cannot succeed regardless of retry speed. Stopping repeated refresh attempts therefore does not make useful work slower; it replaces noisy failures with one actionable status.

After the user logs in again, the revised design takes at most about 20 seconds to resume and typically about 10 seconds. An earlier proposal combined 60-second checks in a way that could make recovery take nearly two minutes. That design is rejected because the delay would be obvious and frustrating to users.

If 20 seconds still feels too slow, the machine-wide probe can run every 5 seconds. That gives near-immediate recovery but starts one CLI subprocess every 5 seconds per affected machine until login is repaired. The recommended starting point is 15 seconds, backed by a recovery-latency metric.

## Layer 2: Allow only one machine-wide refresher

Create a central credential broker in the Omnigent auth module. It does not need to be a permanently running daemon. Any process can become the refresher by acquiring the profile-and-workspace file lock.

The flow is:

1. A process needs a token.
2. It reads the published token file.
3. If that token is still valid, it uses it.
4. If refresh is needed, it acquires the profile lock.
5. After acquiring the lock, it rereads the published file because another process may already have refreshed it.
6. Only if refresh is still needed does it ask Databricks for a new token.
7. It atomically publishes the new token and releases the lock.

This “check again after locking” step prevents sequential duplicate refreshes: processes that waited for the lock use the result produced by the first process instead of refreshing again.

The broker must also protect against `databricks auth login` running at the same time. Before requesting a token, it records a private fingerprint of the profile's credential source. Before publishing, it reads that source again. If it changed, the broker discards its result and retries the normal token path using the newly logged-in credentials. The fingerprint must never be logged or written to shared diagnostics.

Every CLI or SDK operation has a bounded timeout. A timeout is a temporary failure: the operation is cancelled, the lock is released, and no dead marker is created. The operating system releases the lock automatically if the refresher crashes, allowing another process to take over. Add useful diagnostics for command timeouts and unexpected lock contention.

There is an unavoidable narrow crash window after Databricks rotates a credential but before Omnigent publishes the new bearer token. Recovery must still remain serialized under the lock. Record a metric for “refresh started without publication,” and on takeover first run the ordinary token path to discover any usable result before attempting another refresh.

If refresh succeeds but publishing fails, the broker reports a prominent error and keeps recovery serialized. It must not hand the unpublished token to only one long-lived component, and no consumer may fall back to refreshing independently. The next broker attempt first checks whether the publication problem has cleared and whether the ordinary auth path can provide the same usable token.

Runners must never refresh the Databricks profile themselves. They consume the host or broker's published bearer token.

## Layer 3: Publish expiry with the token

Replace a bare-token file with private JSON resembling:

```json
{
  "token": "REDACTED",
  "exp": 1780000000,
  "profile": "oss",
  "workspace_host": "https://example.cloud.databricks.com",
  "published_at": "2026-08-24T12:00:00Z"
}
```

Consumers use `exp` to reread the shared file shortly before expiry rather than waiting for a rejected request. Use a small safety margin for clock differences and network delay.

Readers must treat malformed, mismatched, or expired files as unusable. They must never log the token.

Expiry is only a hint. Databricks can revoke a token before `exp`. After rejection, a consumer rereads the shared file once in case another process already published a replacement. If the token is unchanged, it asks the broker for recovery. It never refreshes directly.

A runner that reconnects or resumes after being disconnected must reread the shared file before its first request. It must not reuse the bearer token retained from its earlier connection.

## Layer 4: Route all Databricks authentication through one module

Create one public Omnigent API for obtaining Databricks credentials and move hosts, runners, forwarding, policy checks, Codex integration, web support, and CLI paths onto it.

Add a repository check that rejects new direct calls to Databricks SDK refresh methods or `databricks auth token` outside the central module and explicitly approved tests. This prevents the same concurrency bug from quietly returning later.

## Layer 5: Prove it under stress

Add an integration or chaos test that starts at least 20 processes against one profile and verifies:

- Exactly one refresh occurs for each token rotation.
- All consumers receive the same usable bearer token.
- Killing the process holding the lock allows another process to take over.
- A permanent error creates one dead marker and stops refresh thrashing.
- Temporary network errors do not create a dead marker.
- A successful re-login removes the marker automatically.
- Hosts recover within about 20 seconds and existing sessions resume in place.
- The forwarder resumes at the saved cursor without loss or duplication.
- No secret appears in logs, marker files, or test output.
- Two sessions with different Omnigent data directories still share one refresh lock.
- The same profile name on two workspaces receives separate state and locks.
- Login racing with refresh discards the superseded result.
- A hung credential command times out and releases the lock without creating a dead marker.
- Unsafe files—including symlinks, wrong ownership or modes, oversized files, and malformed JSON—are rejected.
- A token revoked before its stated expiry follows the broker recovery path.
- A disconnected runner rereads the token before resuming.
- Publication failure never causes runners or other consumers to refresh independently.

Add metrics and structured logs for:

- refresh attempts, successes, and failures by profile and error category
- lock wait time and timeout count
- dead-marker creation and clearing
- time from successful login probe to host recovery
- active marker age

The main success metric is approximately one credential refresh per hour per machine and profile, rather than one per process.

## Implementation plan

All of this work can be delivered in one PR. The steps below are an implementation order, not separate rollout phases or deployments. Keeping them as distinct commits may make review easier, but the final behavior should be enabled together.

### Step 1: Build the shared authentication foundation

- Add the central Omnigent authentication module.
- Define normalized profile-and-workspace identity.
- Resolve the canonical user-state and machine-runtime directories.
- Add safe JSON reading, atomic publication, ownership and permission checks, and reliable file locking.
- Define the shared token, dead-marker, and lock formats.

### Step 2: Implement the broker and dead-state recovery

- Serialize token refresh through the shared lock and recheck the token after acquiring it.
- Handle login/refresh races, command timeouts, process crashes, and publication failures.
- Add the strict permanent-error classifier and `.dead` marker.
- Add the 15-second machine-wide recovery probe and 5-second marker checks.
- Publish token expiry and identity metadata in the shared format.

### Step 3: Move every consumer to the shared path

- Make hosts obtain and publish credentials through the broker.
- Make runners read the shared token at startup, before reconnecting, shortly before expiry, and after rejection.
- Make runners wait briefly for an in-progress publication and remove every path that lets them refresh independently.
- Move forwarding, policy checks, Codex integration, web support, and CLI authentication to the same module.
- Remove old direct-refresh and per-host token-file behavior.

### Step 4: Add consistent user-visible recovery

- Show the same expired-login explanation and login command in every component.
- Add the host status and web banner.
- Preserve forwarder cursors and existing sessions while authentication is unavailable.
- Verify that login restores hosts within about 20 seconds and sessions resume in place.

### Step 5: Add prevention, tests, and diagnostics

- Add the repository check that prevents new direct refresh calls outside the central module.
- Add the 20-process concurrency test, crash takeover test, and all edge-case tests listed above.
- Add metrics and structured logs for refresh count, lock contention, dead markers, publication failures, and recovery time.
- Confirm that normal request latency is unchanged and refreshes converge to one per machine, profile, workspace, and token rotation.

## Safety and failure cases

- **Login races with refresh:** Detect the changed credential source, discard the stale result, and retry against the new login.
- **Refresher crashes:** The operating system releases the file lock; another process checks the ordinary auth path before considering another refresh.
- **Credential command hangs:** Cancel it at the timeout, release the lock, and treat it as temporary failure.
- **Token file is half-written:** Atomic replacement prevents readers from observing partial data.
- **Token file is malicious or unsafe:** Reject symlinks, wrong ownership, weak permissions, oversized content, malformed JSON, and identity mismatches.
- **Wrong profile or workspace:** Reject the file rather than sending a token to the wrong destination.
- **Same profile name targets two workspaces:** Separate state and locks by normalized profile-and-workspace identity.
- **Sessions use different data directories:** They still coordinate through the canonical per-user state and machine-runtime locations.
- **Runtime filesystem cannot provide reliable locks:** Fail clearly; never fall back to uncoordinated refresh.
- **Network is temporarily down:** Back off normally; do not mark credentials dead.
- **Bearer token is revoked before expiry:** Reread once, then ask the broker for recovery.
- **Runner resumes after disconnection:** Reread the shared token before sending its first request.
- **Databricks login is permanently invalid:** Create the marker once and stop repeated refresh attempts.
- **User logs in again:** The next probe validates the credential, clears the marker, and components reconnect.
- **CLI returns an unusable cached token:** Clear only the known bearer cache under the lock and retry once; preserve the dead marker if validation still fails.
- **Several processes probe together:** The lock and marker timestamp allow only one probe in each interval.
- **Refresh succeeds but publication fails:** Keep recovery serialized, report it prominently, and do not allow consumer refresh fallbacks.
- **Machine clock changes:** Use monotonic time for local retry intervals; use wall-clock timestamps only for diagnostics and token expiry validation.
- **Process cannot write the shared directory:** Fail clearly instead of silently reverting to independent refreshes.

## Open decisions

1. Select and test the universal authentication-validation endpoint for every supported Databricks identity. This is an implementation prerequisite.
2. Decide whether the recovery probe should start at 15 seconds or 5 seconds. Fifteen seconds limits subprocess churn and gives a roughly 20-second worst-case recovery.
3. Choose where the web authentication-expired banner appears and whether it offers a copy button for the login command.
4. Choose the runner's short wait time for a newly published token before reporting the shared auth failure.
5. Confirm that 24 hours is the right threshold for escalating a stale-marker warning; it must remain advisory.
6. Decide whether the separate bug that reports credentials as expired on `1970-01-01` belongs in this work or in its own fix.

## Acceptance criteria

- Twenty concurrent processes cause one refresh, not twenty.
- No runner can refresh the shared Databricks profile independently.
- Separate hosts and sessions on the same machine coordinate through the same profile-and-workspace lock even when their Omnigent data directories differ.
- The implementation refuses unsafe shared refresh when reliable machine-local locking is unavailable.
- Permanent credential failure produces one clear state and one recovery instruction everywhere.
- Temporary failures, bearer-token rejection alone, and ambiguous `401` responses never create a dead marker.
- A successful login clears the state automatically and restores service within about 20 seconds.
- Existing sessions resume without being recreated or forked.
- A refresher crash is recovered without manual cleanup.
- A login racing with refresh cannot publish a token derived from superseded credentials.
- A reconnecting runner cannot reuse the bearer token retained before disconnection.
- Normal request latency is unchanged.
- Tokens are never exposed through logs or insecure file permissions.

## Glossary

- **Access token / bearer token:** A short-lived secret sent with a request to prove identity.
- **Refresh token:** A longer-lived secret used to obtain a new bearer token.
- **Rotation:** Replacing a refresh token with a new one when it is used.
- **Reuse detection:** A security rule that may invalidate a token family if an already-replaced refresh token is used again.
- **Token family:** The chain of refresh tokens descended from one login.
- **Profile:** A named Databricks CLI login, such as `oss`.
- **Databricks workspace host:** The Databricks server URL associated with a profile.
- **Omnigent host:** The local Omnigent process connecting sessions to the server; not the Databricks workspace host.
- **Runner:** The process executing a model or agent session.
- **Forwarder:** A process that sends locally produced records or events to the server.
- **Policy hook:** A check that approves or rejects an action before it runs.
- **Fail closed:** Reject an action when a required safety check cannot be completed.
- **Bridge:** The connection layer joining local processes and the remote Omnigent service.
- **SDK:** A software library used to call Databricks APIs.
- **`--force-refresh`:** A CLI option that asks for fresh credentials even if cached credentials may still work.
- **Apps edge / login redirect:** The outer Databricks Apps layer that may redirect unauthenticated requests to login.
- **Marker file:** A small shared file recording that credentials are known to be invalid.
- **Probe:** A rate-limited attempt to determine whether login now works.
- **Broker / refresher:** The shared code path allowed to obtain and publish a new token.
- **Lock / `flock`:** An operating-system mechanism allowing only one local process into a critical section at a time.
- **Atomic write:** Writing a complete temporary file and replacing the old file in one operation so readers never see partial content.
- **Data directory:** A session's configured directory for general Omnigent state. Credential coordination deliberately does not depend on this directory.
- **User state directory:** The one canonical location where all Omnigent processes for an operating-system user share credential metadata.
- **Machine runtime directory:** A private, machine-local location for short-lived locks. Unlike a network-mounted home directory, it must provide reliable local process locking.
- **Credential fingerprint:** A private comparison value used only to detect whether login credentials changed during an operation; it is never logged or published.
- **JWT / `exp`:** A common token format and its expiry-time field.
- **Fail fast:** Stop promptly with a clear error when retrying cannot help.
- **Thrashing:** Repeating expensive work that cannot currently succeed.
- **Backoff:** Increasing the delay between repeated attempts after temporary failures.
- **Thundering herd:** Many processes attempting the same work simultaneously.

## Implementation addendums

This section records intentional differences discovered during the required implementation audits. Unintended differences must be fixed in code instead of being documented here.

### Step 1 audit: shared authentication foundation

- **Opaque tokens have no `exp` value.** JWT bearer tokens publish their real `exp` claim. Opaque personal-access tokens cannot be decoded and therefore publish `exp: null`; treating them as if they had an invented expiry could cause needless refreshes or premature failure. They remain cached until Databricks rejects them, at which point the normal rejection and broker-recovery path applies. Consumers must accept `null` only for opaque tokens.
- **Production coordination ignores session data-directory overrides.** The implementation uses one canonical per-user state directory and one machine-local runtime directory. Separate overrides exist only for isolated tests; production sessions cannot select independent lock locations.
- **Windows ownership checks use platform capabilities.** POSIX validates numeric file ownership and modes. Windows lacks `getuid`, so the implementation relies on a per-user runtime path plus the platform's file access controls and still rejects links, non-regular files, malformed content, and unsafe state shape. Windows-specific access-control verification remains part of the platform test requirement.

### Step 2 audit: broker and dead-state recovery

- **Recovery validation uses the workspace status API.** Before a `.dead` marker is removed, the broker calls `GET /api/2.0/workspace/get-status?path=/` with the candidate bearer. This is a small, read-only workspace request and proves that the intended workspace accepts the token; merely receiving a token from the CLI is not enough.
- **The first recovery probe remains an ordinary token lookup.** It does not force a refresh. If the workspace rejects that returned token, the broker performs one forced OAuth refresh under the same lock and validates the replacement. This handles a token revoked before its advertised expiry without rotating healthy credentials during every probe.
- **OAuth login races receive a second ordinary lookup.** File metadata detects config-file changes, but macOS Keychain and other credential managers can change without touching a visible file. After the first OAuth-U2M lookup, the broker therefore performs one cheap cached lookup and uses the newer result if they differ. This adds negligible cost only to the approximately hourly refresh path, not normal requests.
- **Timeouts follow the credential mechanism.** OAuth-U2M runs in a child process with a 20-second timeout, so a hung CLI can be killed. SDK-backed network credential types are capped through the SDK's HTTP timeout. A timeout is temporary and never creates a `.dead` marker.
- **Probe throttling uses the machine's monotonic clock.** The marker retains wall-clock timestamps for diagnostics, but the 15-second gate uses monotonic time so a manual or automatic wall-clock correction cannot suppress probes or cause a burst. A negative elapsed value, such as after reboot, is treated as stale and allows a new probe.

### Step 3 audit: consumer migration

- **Harness helpers invoke the Python broker.** Generated Claude and Codex token commands no longer call `databricks auth token` or `--force-refresh` themselves. The old recorded-command fallback is accepted as an API argument for compatibility but is deliberately not executed, because it would bypass machine-wide serialization.
- **Runners use the same broker rather than a separate refresh path.** A runner still starts with the bearer handed to it by its host. If Databricks rejects that bearer, the runner invalidates the shared publication and retries once through the broker. The process that wins the broker lock may be a host, runner, or harness helper; ownership is not pinned to the host process, but refresh is still single-file, machine-wide, and serialized. This is intentional: it avoids adding a host-runner control channel while preserving the safety property that only one refresh can occur.
- **Test state is isolated per test.** The test suite points broker state and locks at a private temporary directory for every test. This prevents tests from reading or changing a developer's real credential publication and prevents one test's token from hiding another test's authentication call.

### Step 4 audit: user-visible recovery

- **The browser cannot reliably receive a special host-auth status.** A dead Databricks credential is rejected by the Databricks Apps edge before the host's WebSocket reaches the Omnigent server. The server therefore sees an offline host, but it cannot distinguish “login expired” from sleep, network loss, or a stopped process. Sending that distinction would require a second control channel or heartbeat, which this design explicitly avoids. The implementation keeps the existing offline host badge in the web UI and gives the exact `databricks auth login` command in host, runner, harness, and CLI errors and logs. It does not claim a browser-only “login expired” diagnosis that the server cannot prove.
- **Sessions remain in place during authentication loss.** No session, forwarder cursor, runner record, or local workspace is deleted when the marker is present. Components retry through the shared broker; a successful 15-second probe removes the marker and the existing host and runner reconnect paths resume the same sessions.

### Step 5 audit: prevention, tests, and diagnostics

- **The repository guardrail is syntax-based.** Pre-commit parses every production Python file and rejects any `.authenticate()` call outside `databricks_auth_broker.py`. This catches direct SDK refreshes without false positives from comments or documentation. The broker is the only allowlisted implementation boundary.
- **Concurrency and crash behavior use real processes.** The test suite starts 20 consumers against one profile and workspace and proves only one authentication call occurs. A separate test kills a process while it owns the lock and proves the operating system releases the lock for the next process. Timeout, dead-marker throttling, failed validation, unsafe files, identity separation, and private atomic publication have focused tests as well.
- **Diagnostics use existing structured process logs rather than a new metrics backend.** The broker logs lock waits over 50 milliseconds, successful publication duration, dead-marker creation, recovery, and publication failures. Logs include only profile and normalized host—never bearer tokens, refresh tokens, or credential fingerprints. The repository does not currently have a universal client-side metrics sink, so adding a separate telemetry transport solely for this feature would create more failure surface than it removes.
