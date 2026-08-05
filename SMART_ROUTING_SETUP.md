# Smart Routing: setup and user journeys

Set up Smart Routing on your own machine and walk every user journey it ships.
The examples use the `eng-ml-agent-platform` staging workspace; substitute your
own workspace anywhere it appears. Time to a working stack: ~15 minutes.

## How it decides what you see

Smart Routing has two possible routers, and your credentials pick which one
answers — not whether the feature exists. Two signals feed that choice:

1. **The server's routing sources** (`GET /v1/info` → `smart_routing_sources`):
   `external` is the Databricks AI-Gateway `task_v1` router, `oss` is the
   built-in judge that ships with the server. A Databricks provider gives you
   `external` automatically.
2. **Each harness on your machine** must be AI-Gateway-backed
   (`GET /v1/hosts` → `gateway_inference`, per family) for the *gateway* router
   to be usable there. The gateway router answers with gateway catalog ids, so a
   CLI running off a personal subscription (ChatGPT codex, a plain `claude`
   login, Bedrock) cannot run its picks — the built-in judge serves that family
   instead.

| Your setup | Which router answers | What you see |
| --- | --- | --- |
| `external` configured, **both** families gateway-backed | the AI Gateway `task_v1` router | every surface: the Smart Routing harness row and both Model rows; each chip carries the small Databricks mark |
| `external` configured, one family off the gateway | per decision, over the families it involves: the backed family's Model row keeps the AI Gateway `task_v1` router; the off-gateway family's Model row goes to the built-in judge, and so does the Smart Routing harness row (it involves both) | the same surfaces — nothing disappears; only the judge's decisions lack the mark, and the CLI prints one line: *"… is not AI-Gateway-backed on this host — routing with the built-in router instead"* |
| no `external`, `oss` configured | the built-in judge | every surface; no chip carries a mark |
| neither source configured | nobody | no Smart Routing surfaces at all, and the CLI errors *"the server has no routing model configured"* |

Unknown is not "off the gateway": a host that reports nothing keeps the gateway
router. The full four-state credential matrix (neither/one/both families
backed), with recipes to simulate each state in an isolated config, lives in
`.omnigent-local/GATING_NOTES.md`.

### Which router answered?

The decision chip carries a small Databricks mark when the AI Gateway's router
answered. No mark means the built-in judge answered — pickers are never
branded, only decisions. For the exact answer, expand the raw verdict JSON on
the routing card and read `router_source`: `"databricks-aigw"` or `"oss-llm"`.
Sessions routed before this field existed carry neither, and get no mark.

## What you need first

| Requirement | Check |
| --- | --- |
| A Databricks workspace with the AI Gateway routing API | you can open the workspace |
| `databricks` CLI | `databricks --version` |
| `uv` | `uv --version` |
| Node 20+ / npm | `node --version` |
| `tmux` | `tmux -V` |
| `claude` CLI | `claude --version` |
| `codex` CLI | `codex --version` |

A missing `claude` or `codex` binary hides that harness's Smart Routing
surfaces. That is by design — install both to see everything.

## 1. Clone and build

```bash
git clone git@github.com:omnigent-ai/omnigent.git
cd omnigent
git checkout routing-mvp-v3
uv sync
(cd web && npm install)
```

## 2. Log in to the workspace

```bash
databricks auth login --profile eng-ml-agent-platform
```

Both the router and both harnesses authenticate through this OAuth profile.
Tokens are minted per call, so the ~1 hour OAuth expiry never interrupts a
session. (This is why the config below uses `profile:`, never `api_key:`.)

## 3. Create the config

This is the only file the repo does not carry (it names your credentials).

```bash
mkdir -p .omnigent-local
```

Write `.omnigent-local/config.yaml`:

```yaml
providers:
  eng-ml-agent-platform:
    default: true            # default for BOTH model families -> claude + codex gateway-backed
    kind: databricks
    profile: eng-ml-agent-platform

routing:
  provider: external
  base_url: https://eng-ml-agent-platform.staging.cloud.databricks.com/ai-gateway/routing/v1
  router_name: task_v1
  profile: eng-ml-agent-platform
  model_prefix:
    - databricks-
    - system.ai.
```

Notes:

- The `routing:` block is technically optional on a Databricks provider (the
  server derives one), but declaring it makes the target explicit.
- To run **without** Smart Routing, set `routing: {provider: none}`.
  Deleting the block does **not** disable routing — the server derives a
  client from the default provider.
- To back only one family with the gateway (e.g. gateway Claude + ChatGPT
  codex), scope the provider with `default: anthropic` instead of
  `default: true`. See GATING_NOTES §3 for the full recipes.

## 4. Verify credentials before launching

```bash
source dev-env.sh
uv run python -c 'from omnigent.gateway_inference import gateway_inference_map as m; print(m())'
```

You want `True` for both families:

```
{'claude-native': True, 'native-claude': True, 'codex': True, 'codex-native': True, 'native-codex': True}
```

A `False` family means that harness cannot run the AI Gateway router's picks,
so its decisions fall to the built-in judge (or error, if the server has no
built-in judge). Most common cause: the codex CLI is signed into a ChatGPT
subscription instead of the gateway.

## 5. Start the stack

Three terminals (each script sources `dev-env.sh`; the server defaults to
port 50151):

```bash
# terminal 1 — server
./run-server.sh

# terminal 2 — host daemon (registers this machine, launches terminals)
./run-host.sh

# terminal 3 — web UI
cd web && OMNIGENT_URL=http://localhost:50151 npx vite --port 5174
```

## 6. Confirm it is healthy

```bash
curl -s localhost:50151/v1/info  | jq '{smart_routing_enabled, smart_routing_sources}'  # true, external true
curl -s localhost:50151/v1/hosts | jq '.hosts[0] | {status, gateway_inference}'         # online, both true
```

---

## 7. The user journeys

Each journey below says what to do and exactly what you should see. Do them in
order the first time — later ones build on concepts from earlier ones.

For every CLI journey, use the checkout's own binary (`uv run omni …`). A
globally installed `omni` against this server **will** break in confusing ways
(version skew).

### CUJ A — Smart Routing as the harness (the fully-auto session)

Web UI (http://localhost:5174): new chat → harness picker → **Smart Routing**
→ type a first message and send.

Expect: the router reads your message and lands the session on a concrete
harness + model (e.g. Claude Code on `claude-opus-4-8` for a gnarly prompt,
Codex on `gpt-5-6-luna` for a trivial one). One routing chip under the message
shows the pick and the rationale. The session then behaves as a normal session
of that harness — later messages do not re-route.

### CUJ B — Smart Routing as the model (pinned harness)

New chat → harness **Claude Code** (or Codex) → gear → **Model → Smart
Routing** → send a first message.

Expect: the session keeps your harness; only the model is routed. One chip,
one decision, model pinned for the rest of the session. Trivial prompts land
the cheap arm; sprawling ones escalate.

### CUJ C — first-message routing from a bare terminal (claude)

```bash
uv run omni claude --smart-routing --server http://localhost:50151
```

No prompt flag — the TUI opens unrouted. Type something trivial
(`what is 2+2?`) and press enter.

Expect, in the pane, in order: a hook notice — *"Smart Routing selected
&lt;model&gt;; rerunning your message on it"* — then a bare `/model` line, then
*"Set model to &lt;tier&gt; for this session only"*, then your prompt replayed
once, then the answer on the routed model. The status line shows the routed
tier. Type a second message: no re-route (the decision is per-session).

Two guarantees to spot-check: `md5 ~/.claude/settings.json` before and after —
identical (routed switches never touch your global default); and the web view
of the session shows exactly one user message per prompt (the replay does not
duplicate).

### CUJ D — first-message routing from a bare terminal (codex)

```bash
uv run omni codex --smart-routing --server http://localhost:50151
```

Type a trivial prompt.

Expect: the block notice, then the answer on the cheap arm. `/model` inside
the TUI highlights the routed model as `(current)` — the switch is applied in
codex's own model spelling, so there is no "model metadata not found" warning
for the routed model. Second message: no re-route.

### CUJ E — routed subagents (the spawn chips)

In any session from CUJ A/B (routing on): ask the agent to spawn subagents,
e.g. *"spawn subagents to: 1. say hi back to me, 2. add a --dry-run flag to
the deploy CLI"*.

Expect: one routing chip per spawn, scored per task — the greeting lands the
cheapest arm, code-shaped delegate work can land `glm-5-2`, harder tasks the
default arm. The chip names the subagent and the model.

### CUJ F — strict router adherence (requested models don't bypass routing)

Same session: ask it to spawn a subagent *"using model gpt-5.6-sol"* on a
trivial task.

Expect: the router is still consulted. If it agrees, the chip's rationale ends
*"Spawn requested …; honored — the router picked the same arm."* If it
disagrees, the spawn runs on the router's pick and the chip shows the
requested model struck through next to the applied one, with *"overridden —
the router picked …"* in the rationale. An explicit ask is honored only when
the router independently lands the same arm.

### CUJ G — the subagent routing knob

Open the session's gear. **Subagent routing** is the one in-session routing
control (two options: Smart Routing / Default). Sessions that started with
Smart Routing are already set to Smart Routing — stamped at create, no action
needed.

Set it to **Default**, save, and spawn again: no chip; the spawn runs on
whatever the harness natively does. Set it back: the next spawn routes. Note
there is no in-session toggle for the session's *own* routing — that choice
happens once, at session start.

### CUJ H — bundle-agent brains (Polly / Debby)

New chat → pick **Polly** → gear → **Agent Harness → Smart Routing** → save →
send a first message.

Expect: the chip and modal keep saying "Polly" — Smart Routing here is a knob
on Polly (the router picks her brain harness + model per session), not a
different top-level selection. In-session, Polly's gear shows only the
Subagent routing row (CUJ G); her own routing was decided at create.

### CUJ I — cross-family subagents (auto sessions only)

In a CUJ A session (Smart Routing harness), ask for a spawn that suits the
other family.

Expect: the router may cross families — you'll see the child session appear on
the other harness with its own routing decision, and the result return to the
parent. In a CUJ B session (pinned harness), spawns never cross families:
picks stay in-family or run as-is. This asymmetry is deliberate — pinning a
harness is a promise routing keeps.

### CUJ J — partial credentials (what other people will see)

If your gateway backs only one family, the surfaces all stay — the unbacked
family's decisions, and the Smart Routing harness row's (it involves both
families), come from the built-in judge instead, and their chips carry no
Databricks mark. The backed family's own Model row still gets the gateway
router, mark and all. The CLI says so once per launch (*"not AI-Gateway-backed
on this host — routing with the built-in router instead"*). Surfaces only
disappear when the server has no built-in judge either, and then the two errors
are specific — *"not AI-Gateway-backed"* (a credential problem on this machine)
vs *"the server has no routing model configured"* (a server problem). Recipes
to simulate every state without touching your real config: GATING_NOTES §3.

---

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| No "Smart Routing" harness row | Only happens when **neither** router can serve it: a family is off the gateway *and* the server has no built-in judge. Re-run step 4, and check `smart_routing_sources` in step 6. |
| Codex Model row has no Smart Routing option | Codex is on a ChatGPT subscription *and* the server has no built-in judge. See step 4; configure a built-in judge and the row stays, answered by that judge. |
| CLI: "not AI-Gateway-backed" | Same cause, caught at preflight. With a built-in judge the CLI instead prints *"routing with the built-in router instead"* and carries on. |
| CLI: "the server has no routing model configured" | The server has no routing client — check the `routing:` block and step 6. |
| First `omni claude …` errors "did not create the Claude terminal within 60s" | Cold-start race on the very first runner spawn. Retry once. |
| UI answers 502 everywhere | vite was started without `OMNIGENT_URL`. Restart terminal 3 as written. |
| Deleting the `routing:` block did not disable routing | It does not — use `routing: {provider: none}`. |
| "Model metadata for `databricks-…` not found" at codex **launch** | Known cosmetic gap on the launch model only; routed switches are unaffected. |
| "Stop hook (failed): hook exited with code 1" in a pane | Your own user-level CLI hooks, not omnigent. |
| Chips show a routed model you didn't expect | Read the chip's rationale — it is the router's own rule trace (e.g. "trivial task → cheapest arm"). |
