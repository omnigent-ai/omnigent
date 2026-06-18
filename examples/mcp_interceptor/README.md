# MCP Interceptor — live PDP enforcement demo

This example proves the [`mcp_interceptor`](../../omnigent/policies/builtins/mcp_interceptor.py)
policy end-to-end against a **live** Policy Decision Point (PDP) that implements
the MCP Interceptor JSON-RPC protocol
([modelcontextprotocol/experimental-ext-interceptors](https://github.com/modelcontextprotocol/experimental-ext-interceptors)):

```
agent tool call ─▶ omnigent policy engine ─▶ mcp_interceptor policy
                                                   │  POST interceptor/invoke
                                                   ▼
                                 MCP Interceptor PDP ──▶ ValidationResult {valid, severity}
                                                   │
        DENY / ASK / ALLOW  ◀── verdict mapping ┘   (severity:error→DENY, valid:true→ALLOW, warn→on_notify)
```

When the validation returns `severity: "error"`, the tool call is denied and
never executes; the agent receives a `[Denied by policy: …]` sentinel carrying
the PDP's reason.

## What's here

| File | Purpose |
|------|---------|
| `config.yaml` | The agent: claude-sdk harness, a native shell tool, and the `mcp_interceptor` policy pointed at a live PDP. |
| `check_pdp.py` | No-LLM smoke test — calls the real policy against the live PDP and prints the verdict. Run this first. |

## Prerequisites

1. **omnigent installed** in a Python ≥3.12 env (see the repo README; for a quick
   editable install use `OMNIGENT_SKIP_WEB_UI=true uv pip install -e '.[dev]'`).
2. **A PDP** implementing the MCP Interceptor spec, reachable from this machine,
   and a **bearer token** for it.
3. **An LLM credential** for the agent. This example defaults to your **Claude
   Code subscription** via the claude-sdk harness — just have the `claude` CLI
   installed and logged in (`claude` on `PATH`, `~/.claude` populated) and leave
   `ANTHROPIC_API_KEY` unset. (To use Databricks model serving instead, edit
   `config.yaml` per its header comment.)

## 1. Configure the PDP

Set the `endpoint:` in `config.yaml` (and `--endpoint` for `check_pdp.py`) to
your PDP URL. The token is sent as `Authorization: Bearer <token>` and read from
the `MCP_INTERCEPTOR_TOKEN` env var:

```bash
export MCP_INTERCEPTOR_TOKEN=<your-pdp-bearer-token>
```

## 2. Smoke-test the PDP (no LLM)

Confirms the endpoint, token, and policy wiring before spending an LLM turn:

```bash
python examples/mcp_interceptor/check_pdp.py \
  --endpoint https://your-pdp.example.com/api/interceptor --tool send_email
```

Expected (against a PDP whose entity-catalog policy is active):

```json
{
  "result": "DENY",
  "reason": "The call was denied due to N policy violations.; … Tool send_email is not registered in the entity catalog"
}

verdict: DENY
```

## 3. Run the full agent live

```bash
python -m omnigent run examples/mcp_interceptor \
  --no-session \
  -p "What is the current time? Use the shell to run: date"
```

The model calls the native `sys_os_shell` tool, the policy forwards it to the PDP,
the PDP returns `block`, and the agent reports the denial — for example:

```
That tool call was blocked by policy. The reason given verbatim is:
> "The call was denied due to 3 policy violations.; … Tool sys_os_shell is not
>  registered in the entity catalog"
```

The `date` command never runs. Swap in a tool your PDP *allows* (one registered in
its entity catalog) and the same agent proceeds normally — that's the ALLOW path.

## 4. Run it in the web UI (browser)

The same agent runs in omnigent's web UI.

**a. Build the web UI once** — writes into `omnigent/server/static/web-ui/`, which
the server then serves at `/`. The clean reinstall clears the npm optional-deps
bug that otherwise breaks Vite's rolldown native binding:

```bash
cd ap-web
rm -rf node_modules package-lock.json
npm install
npm run build
cd ..
```

**b. Start the server with the agent + token** (terminal 1):

```bash
export MCP_INTERCEPTOR_TOKEN=<your-pdp-bearer-token>
python -m omnigent server --agent examples/mcp_interceptor
```

**c. Register this machine as a host** (terminal 2) — `os_env` tools run on a
host, and the policy executes there too, so it also needs the token:

```bash
export MCP_INTERCEPTOR_TOKEN=<your-pdp-bearer-token>
python -m omnigent host http://localhost:6767
```

**d. Open `http://localhost:6767`** and start a session:

1. Agent selector → **Mcp-interceptor-demo**
2. Host → your machine (shows **ONLINE**)
3. Working directory → **`/tmp`** (the agent pins `os_env.cwd: /tmp`; set
   `cwd: .` in `config.yaml` to allow any directory instead)
4. Prompt: `What is the current time? Use the shell to run: date` → send

The agent calls the `date` shell tool, the policy forwards it to the live PDP,
and the conversation shows the call **blocked** with the PDP's reason — the
shell never runs.

## Notes

- **Web UI needs a host.** A bare `omnigent server` has no runner, so the
  `os_env` shell tool has nowhere to run; register one with `omnigent host`
  (step 4c). The server *and* the host must both carry `MCP_INTERCEPTOR_TOKEN` —
  policy evaluation happens on the host/runner.
- **`phase` is required.** `interceptor/validate` rejects calls without a
  `phase` (`request` for tool calls, `response` for tool results); the policy
  sends it automatically. Omitting it returns `-32602 "Invalid phase"`.
- **Why native `os_env` tools** (not a `type: function` tool): the claude-sdk
  harness does not currently surface YAML FunctionTools to the model (the model
  reports the tool as an "unknown skill"), but it *does* expose native os_env
  tools, and those route through the policy engine. With other harnesses
  (e.g. `databricks_supervisor`) a `type: function` tool works too.
- **`sandbox.type: none`** is set so the os_env tool runs without the default
  bubblewrap re-exec, which can't import an editable install. It also means the
  shell is unsandboxed — fine here because the policy denies the call. Use a real
  sandbox for anything that should actually run.
- **`on_notify`** maps the advisory `notify` status to a verdict
  (`deny` here; `ask` to prompt for approval; `allow` for audit-only).
- **Fail-closed:** if the PDP is unreachable or returns a bad response the policy
  DENYs by default. Set `fail_open: true` in `arguments` to allow instead.
