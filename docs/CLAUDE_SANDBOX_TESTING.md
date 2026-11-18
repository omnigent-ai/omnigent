# Claude sandbox CLI verification

The CLI integration tests run the real system and SDK-bundled Claude binaries
against a local TLS server that implements Anthropic streaming responses. They
use synthetic credentials and do not require an Anthropic account.

Real CLI integration cases are opt-in: set `OMNIGENT_CLAUDE_CLI_E2E=1` to run
them. Without that exact value, they skip; policy unit tests remain available
without the gate.

Run them in a clean Linux worker with bubblewrap, libseccomp, tmux, the test
dependencies, and both Claude CLI binaries installed. Run as a regular user. The
public `omnigent run` tests require an available loopback port 443 and permission
to bind it. Run this matrix serially in a dedicated worker to avoid port
collisions. A Docker worker also needs permission to create bubblewrap's mount,
PID, and network namespaces and mount its private `/proc`; Docker's default
security profile usually blocks that. Omnigent's own sandbox remains enabled
throughout.

Startup fails closed if sandbox write grants cover Omnigent's Python
interpreter, runtime libraries, or framework code and imports. A contained
command must not be able to poison trusted code used before containment on the
next startup. Run Omnigent from a separate installation and virtualenv outside
those write grants; an editable installation inside a writable workspace can
therefore be rejected.

The automatic skill overlay exposes only bundled skills and their supporting
files. It excludes the original bundle's credentials-bearing root configuration
and MCP configuration, including `config.yaml` and `.mcp.json`. The matrix
checks that supporting files remain usable while those root files stay hidden.

```sh
OMNIGENT_CLAUDE_CLI_E2E=1 uv run --no-sync pytest -q -rs \
  tests/inner/test_claude_worker_containment.py \
  --junitxml=/tmp/claude-cli.xml
```

The Linux matrix was verified with SDK-bundled Claude **2.1.169** and system
Claude **2.1.325**, including 29 real CLI cases. The evidence uses a local TLS
fake provider, not live Anthropic or Fargate.

The execution-path matrix covers both CLI discovery paths:

| Entry point | Required verification |
| --- | --- |
| Native Bash | Contained child runs; tool output reaches the next model request. |
| Inline Skill shell expansion | Inline command runs and writes its workspace marker. |
| `SessionStart` command hook | Enabled hook's child produces a containment report. |
| Plugin stdio MCP | CLI launches a local MCP server; its child reports containment. |
| `apiKeyHelper` child | Contained helper runs and returns only the session placeholder. |
| SDK startup | Forwarding shim records the environment for `-v` and main invocations. |
| Sandboxed tmux shell | Typed command is contained; terminal Claude completes inference. |
| Public CLI | Inference, Bash, and `sys_os_shell` callback round trips succeed; callback shell also runs Claude inference. |
| Public terminal tools | `sys_terminal_launch` and `sys_terminal_send` run terminal Claude. |

The SDK forwarding shim executes the real CLI after recording its environment.
Both the SDK's `-v` version check and its main invocation must receive a session
placeholder and private HOME, with neither the inherited host bearer token nor
the real provider key. The real provider credential stays in the trusted parent,
including when the CLI exercises an `apiKeyHelper` child.

The CLI relay also requires a session-specific proxy capability before it can
resolve or forward credentials. Claude reads authenticated proxy URLs from an
explicit settings file with mode `0600` in its private runtime directory, after
startup; the capability stays out of the CLI's initial environment, arguments,
and serialized sandbox policy. Commands launched by Claude inherit that scoped
proxy access. The shared-loopback regression verifies unauthenticated clients
receive `407` before any credential resolution or upstream traffic, including
ordinary swap-on-access credentials alongside automatic model access.
On macOS, the baseline Seatbelt profile blocks process-argument sysctls and
other processes' PID information. Commands can inherit the scoped proxy URL;
the sibling-sandbox probe verifies they cannot recover it from those commands'
process environment, even when the sibling has general network access.

Terminal cases use automatic model access without a model egress rule. The
public terminal-tool case calls `sys_terminal_launch`, then `sys_terminal_send`
to run a containment probe and the actual Claude CLI inside the sandboxed tmux
terminal; it verifies terminal inference as well as the outer tool round trip.

Containment probes must positively establish that the entry point ran, then
check private-file denial, absence of real credentials in the child, blocked
direct networking, and absence of writes outside the host filesystem grants.
Linux uses a private `/tmp`: an ungranted temporary write may succeed privately
or be denied, depending on the path. Either outcome is valid only if the
corresponding host path remains absent; a successful workspace write establishes
that permitted execution still works.

The matrix also checks streaming inference with API key, bearer, and OAuth
credentials retained in the parent, and two SDK turns that preserve context.
OAuth exercises the CLI's normal login flow; `--bare` skips that flow. Every
inference case must return `CLI_E2E_OK`; tool round trips also verify filesystem
effects and the tool result in the following API request. Public CLI cases run
`python -m omnigent run agent/config.yaml -p ... --no-session` through production
spec loading, server, runner, DNS, TLS, and relay code, without transport
monkeypatches in those subprocesses.

On macOS, the containment probes use Seatbelt. The login-Keychain test adds a
synthetic item under a unique account and service in the user's default Keychain
and deletes that item afterward. This uses the same generic-password API as
Claude without sharing its credential service or querying real credentials. It proves the
parent can retrieve the item and look up `com.apple.SecurityServer`, while the
contained child can do neither. The opt-in real-CLI hook test repeats those
checks from a `SessionStart` hook for both CLI binaries. The OS callback test
separately checks a temporary file Keychain through the real shell callback.
Seatbelt still allows other Mach service lookups for macOS runtime compatibility;
the credential boundary specifically denies `com.apple.SecurityServer` and
service names matching `securityd`, `secd`, or `keychain`. It does not claim to
isolate every macOS IPC service.
The networking probes also start from policies with networking enabled, so they
verify that installing the relay removes direct network access.

On macOS, full CLI inference cases skip if machine-managed Claude settings pin a
different provider; use a clean worker instead of changing managed settings.
Missing CLI binaries also produce explicit skips. Check the `-rs` output before
treating a run as complete verification.

Run the supporting policy and relay regressions with:

```sh
uv run --no-sync pytest -q \
  tests/inner/test_sandbox.py \
  tests/inner/test_bwrap_sandbox.py \
  tests/inner/test_claude_model_access.py \
  tests/inner/test_claude_managed_sandbox.py \
  tests/inner/test_claude_sdk_executor.py \
  tests/inner/test_claude_gateway_shim.py \
  tests/inner/egress/test_proxy.py
```

Managed-policy tests use temporary JSON and XML/binary plist files. They check
settings-only directories, drop-ins, and macOS MDM profiles without reading or
changing host policy. Credential-bearing policy and invalid files fail closed;
benign policy stays intact.

This boundary applies to `claude-sdk` with an active `os_env` sandbox. Omitting
`os_env` or selecting `sandbox.type: none` does not request this containment;
skills, hooks, and local MCP commands can then run uncontained.
The CLI wrapper contains the Claude
process and its subprocess descendants. The `sys_os_shell` callback executes
through Omnigent's separately sandboxed OS environment; the public CLI test
checks that callback boundary too. Callback helpers inherit the same credential
protections and automatic inference relay, including Keychain denial on macOS.
The terminal tools launch a separately sandboxed terminal process tree with its
own automatic model relay. Arbitrary registered in-process Python tools execute
in the parent, and remote MCP servers
execute elsewhere; neither is contained by the CLI wrapper.

These tests verify CLI interoperability and OS containment against the local
TLS fake. They do not verify live Anthropic authentication, OAuth token refresh,
Fargate kernel behavior, or `claude-native`.
