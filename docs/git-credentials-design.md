# Git credentials in sandboxes — design (issue #2125)

Grounded against `main` @ `2ede6020`. Written for the design gate that closed #2689 and
paused #2758. Eight design decisions are settled (§6), sliced into nine PRs (§7); four
questions remain (§8). Rejected designs and the reasons they were rejected are in the
appendices — §A (whole-shape alternatives) and §B (mechanism-level ones).

Line citations are repo-relative and drift as `main` moves; prefer the named symbol over the
number.

---

## 0. The problem in plain words

A sandbox is a box we run an agent in. To do useful work the agent needs to talk to
GitHub — clone, push, open a PR. To do that it needs a key.

**Today there is exactly one key, and everybody uses it.**

```
   ┌──────────────────────────────────────────────┐
   │  SERVER  knows who you are                   │ ← you logged in
   │          (Alice? Bob? refactor-bot?)          │
   └────────────────────┬─────────────────────────┘
                        │
                        │  then forgets, because there is
                        │  only one place to keep a key  ✗
                        ▼
                  ONE GIT_TOKEN
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
       Alice's        Bob's         bot's
        box            box           box
          └─────────────┴─────────────┘
                        │
                        ▼
              git push → all three land as the SAME person
```

So: GitHub cannot tell Alice's work from Bob's. Nobody can be given less access than
anybody else. And you cannot use two forges at once, because the one key answers every
host it is asked about.

**And inside the box, the key sits out in the open.**

```
   ┌──────────── the box ─────────────┐
   │  GIT_TOKEN=ghp_realkey           │
   │        ├──→ git          (fine)  │
   │        ├──→ the agent    can read it
   │        ├──→ git hooks    can read it
   │        └──→ the terminal can read it
   └──────────────────────────────────┘
```

Anything running in the box can read the key and send it anywhere. If you treat the agent
as untrusted — and the whole point of a sandbox is that you do — this is a hole.

### Why one missing piece explains all of it

The server knows who you are. The thing that needs to know is the **runner** — the process
that actually starts git. There is no pipe between them.

```
   ┌──────────┐                            ┌──────────┐
   │  SERVER  │   ← NO PIPE EXISTS →       │  RUNNER  │
   │ knows WHO│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─►  │ needs WHO│
   └──────────┘                            └──────────┘
```

No pipe → no per-person key → so no per-host key either → so the only way in is the
environment, out in the open → so nothing knows who wrote a commit → and `gh` and MCP have
the same problem. **Build the pipe and all five fall over together.**

### The shape of the fix: four layers

```
  1  PRINCIPAL    "this session runs as ___"   (a person OR a bot)
                            │
  2  STORE        keys filed by (who, which host, label)
                            │
  3  DELIVERY     ★ THE PIPE ★  server → runner, privately
                            │
  4  CONSUMPTION  plug it into the right socket
```

Layers 1 and 3 are where the two review blockers landed: layer 1 needs a *binding* the
session carries, not just a table (D1), and layer 3 has one more process boundary in it than
the first draft admitted (D8).

Layer 4 needs four different sockets, which is why delivery and use have to be separate
layers — you cannot sign a commit through an HTTP proxy:

```
              ┌─── one pipe (layer 3) ───┐
              └────────────┬─────────────┘
      ┌────────────┬───────┴─────┬─────────────┐
      ▼            ▼             ▼             ▼
   HTTPS        SSH key       GPG key      identity
   token                                   GIT_AUTHOR_*
   proxy swaps  agent socket  agent signs  from the
   on the way   into the box  the commit   principal
      │            │             │             │
      ▼            ▼             ▼             ▼
   git + gh     ssh remotes   signed        commits
                              commits       say who
```

### The trick that keeps the key off the table

This is already built in-tree. `gh` refuses to make a request unless it sees something
key-shaped nearby, so we show it a **fake** and swap in the real one on the way out:

```
   BEFORE                        AFTER
   ┌──── box ─────┐              ┌──── box ─────┐
   │ GIT_TOKEN=   │              │ GH_TOKEN=    │
   │  ghp_realkey │              │  oa_cred_fake│ ← a decoy
   │      │       │              │      │       │
   │ agent sees   │              │ agent sees   │
   │ the real key │              │ only a decoy │
   └──────┼───────┘              └──────┼───────┘
          ▼                             ▼
     github.com                  ┌──────────────┐
                                 │ proxy swaps  │ ← trusted; the agent
                                 │ decoy → real │   cannot reach it
                                 └──────┬───────┘
                                        ▼
                                   github.com
```

The proxy also **refuses** a decoy replayed at the wrong host (403), so a stolen decoy is
worthless (`omnigent/inner/egress/proxy.py:711`, the cross-host leak guard).

**This works because there is a real fence inside the box.** The runner parent runs the
proxy and holds the real key; the agent runs as a separate, confined child. People assume a
container is one trust domain. Here it is not, and that single fact is what makes the whole
design possible.

---

## 1. Two different things are called "sandbox"

Both appear in config under similar names. Mixing them up leads to wrong conclusions, so:

```
┌─ sandbox.provider ────────── WHERE the box runs ─────────────────┐
│  kubernetes | modal | daytona | e2b | boxlite | islo |            │
│  cwsandbox | openshell | lakebox       (managed_hosts.py:154)     │
│                                                                   │
│   ┌───────────────────────────────────────────────────────────┐   │
│   │  omnigent host (trusted parent) + egress proxy in-process  │   │
│   │                                                            │   │
│   │  ┌─ os_env.sandbox.type ── HOW bash is confined ────────┐  │   │
│   │  │  linux_bwrap | darwin_seatbelt | none                │  │   │
│   │  └──────────────────────────────────────────────────────┘  │   │
│   └───────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────┘
```

- **`sandbox.provider`** — which service hosts the box. An operator concern.
- **`os_env.sandbox.type`** — how the agent's shell is fenced *inside* that box. An
  agent-spec concern.
- **OIDC** is neither; it is how a user logs in to the server (`omnigent/server/oidc.py`).

They nest for real: the host image installs `bubblewrap` (`deploy/docker/Dockerfile:213`), so a k8s Pod
runs `omnigent host`, which spawns runners that bwrap the agent's shell.

**Why it matters.** `omnigent/spec/parser.py:976` gates the credential proxy on the **inner** layer. The
requirement is *not* "you must be on Kubernetes" — it is **"the agent's bash must be fenced,
so a boundary exists between the parent holding the key and the child making requests."**
Three consequences:

1. **This is not a provider-parity problem.** Any provider whose image ships `bwrap`
   works — that is the standard image, so all Linux providers qualify.
2. **`sandbox.type: none` is the real exclusion.** No fence means nothing to protect the
   key from. Fail closed there.
3. **macOS is excluded for `gh` only.** `gh` is a Go binary and Go on macOS checks TLS
   against the system store, so it will not trust the proxy CA (`omnigent/spec/parser.py:1724`). Git on
   seatbelt is fine.

---

## 2. The five problems

The issue says "multi-host git credentials." That is one symptom of five tangled problems.

| # | Problem | Who is blocked |
|---|---|---|
| 1 | **One key per deployment.** The image's baked helper answers *every* host from one `GIT_TOKEN`, so github.com and a self-hosted Forgejo cannot coexist — and a GitHub PAT gets offered to the self-hosted remote. | self-hosters (the filed issue) |
| 2 | **One key per deployment, not per person.** Every box in a multi-user deployment pushes as the same identity. | enterprise (issue comment, Slack) |
| 3 | **The key is out in the open inside the box.** The agent, its hooks, submodule fetches and the terminal can all read and leak it. | anyone treating the agent as untrusted |
| 4 | **Commit identity is unmanaged.** No `user.name` / `user.email` / `GIT_AUTHOR_*` anywhere; commits get the image default. Signed commits sit downstream of this. | anyone reviewing agent commits |
| 5 | **The path is git-only.** `gh`, MCP servers and any other per-person key have the same shape and no answer. | both field requests |

They are **separable**, which is the most useful thing to know before choosing. It is also
why the paused stack grew: #2758 attacks 1+2+3 at once — about 2,900 lines of code plus
7,600 lines of design docs, one indivisible review.

---

## 3. What the field actually asked for

Two independent requests landed after the design gate.

**Request A — Slack. OSS omnigent on k8s, GitHub OIDC login.** *"As an admin I can add
`GIT_TOKEN` to the k8s secret, but this gives the same git identity to every user."* Wants
**Settings > Git** in the UI for a per-user PAT or SSH key, and says the same question
applies to **MCP** — a shared service account loses "granular access control, auditing, and
cost control as they all share the same metered usage." Workaround being considered: drop
managed sandboxes, give every user their own long-lived host.

**Request B — async agents on an issue queue.** An orchestrator starts one session per
queued issue, and **each agent has its own identity**. *"I can't figure out how to deliver
different GitHub credentials for the `gh` CLI to different sandbox containers depending on
the identity of the agent they're running."* Suggests **an external credential broker,
like the MCP broker.**

### What these change about the design

1. **The principal is not always a person.** Request B's principal is a *bot* with its own
   PAT, picked per session by an orchestrator over an API, with no human in the loop.
   #2758 keys credentials by `(workspace, owner_user_id, host, label)` and resolves *the
   session owner's* row — that cannot express "this session runs as `refactor-bot`." Key on
   an **opaque principal id** instead and both requests are one feature. This is a schema
   choice: cheap now, a migration later.
2. **`gh` matters as much as `git`.** And `gh` **cannot** work by pure swap-on-access — it
   stops with "authentication required" before touching the network. It needs something
   key-shaped in `GH_TOKEN`. The decoy mechanism already handles this. But any design that
   only teaches the *git credential helper* about hosts does nothing for `gh`.
3. **A broker is being asked for by name.** `ServerMcpPool` (`omnigent/server/mcp_pool.py`) is
   already a server-owned pool that proxies calls with policy — the same shape. That moves
   a broker from "correct destination, not justified yet" to "somebody's actual request."
4. **OIDC is not free.** Login proves who a user is; it does not hand you a git token
   unless the deployment runs a GitHub App that can mint one. Honest answer to A: pasted
   PAT now, OIDC-derived tokens need a GitHub App later.

### The gap neither the issue nor the PRs covered

Both requests want the key to work for `gh` and MCP inside a managed box, and the existing
proxy machinery has two walls there:

- **It is agent-spec-declared, not server-delivered.** `credential_proxy` is a field of
  `os_env.sandbox` in the agent spec (`omnigent/spec/parser.py:975`), and its sources are `env`/`file`/
  `command` read from the **box's own** environment (`omnigent/inner/datamodel.py:378-397`). There is no
  way for the *server* to say "bind github.com to principal P's token." **That hop is the
  load-bearing piece** — not the multi-host helper.
- **It requires egress filtering to be on.** The proxy needs non-empty `egress_rules`
  (`omnigent/spec/parser.py:984`) because the proxy is what performs the swap. So "deliver a key" and
  "turn on egress filtering" are coupled today. Decided in §6, D3.

---

## 4. What already exists on `main`

**Today's path, end to end:**

- **Baked helper** — `deploy/docker/Dockerfile:233` installs a `--system` shell credential helper that
  answers `git credential get` from `$GIT_TOKEN`. **Host-blind by construction:** it offers
  the token to whatever host asks. A repo with a bad submodule URL gets your org PAT. This
  one is not a design that aged badly; it is an unlocked door.
- **Server → box** — the token is a *name* in operator config, read from the server's own
  environment at provision time: `sandbox.<provider>.env`, or
  `sandbox.kubernetes.secret_name` projected via `envFrom`.
- **Clone** — exec providers clone *after* the box exists via `materialize_workspace`
  (`omnigent/onboarding/sandboxes/base.py:860`), whose only channel is `run(sandbox_id, command: str)`. Kubernetes clones
  *before the host exists*, in an init container (`omnigent/onboarding/sandboxes/kubernetes.py:363`).
- **Host → runner** — `GIT_TOKEN` / `GIT_USERNAME` are allowlisted by name in
  `_BASE_HARNESS_CREDENTIAL_ENV_VARS` (`omnigent/host/connect.py:478-497`) and forwarded into the runner
  environment. This is why the key is ambient.
- **Wake** — `resume_managed_host` relaunches with `repo_url=None`
  (`omnigent/server/managed_hosts.py:2565`): the volume still holds the workspace, so no re-clone. Anything
  key-shaped must be re-delivered here, not only at first launch.

**Worth being fair about this code.** The handling is careful — the k8s launch token rides
a Secret via `secretKeyRef` and never the Pod spec, with the reason written down
(`omnigent/onboarding/sandboxes/kubernetes.py:24`); `kubernetes.py:267`, `:1091` refuses credential-shaped names in literal
Pod env; telemetry redacts secret-ish keys (`omnigent/telemetry.py:123-131`). It is a correct
implementation of *one user, one token, one trust domain* — right for the laptop it was born
on. What is wrong is the **model**, not the hygiene. We are adding the missing primitive, not
cleaning up a mess.

**Five things any design should reuse rather than rebuild:**

1. **A secretless credential proxy.** `omnigent/inner/credential_proxy.py` + `omnigent/inner/egress/proxy.py`
   already do exactly what we want: parent resolves the real secret, the MITM proxy attaches
   it outbound, the box holds a decoy or nothing. A `git_https` type already exists
   (`omnigent/spec/parser.py:1401`).
2. **A per-launch secret channel on k8s.** `_token_secret_name` → `"<pod>-token"`, created
   before the Pod, projected by `secretKeyRef`, deleted with the Pod (`kubernetes.py:352`,
   `:1230`, `:655`, `:1541`).
3. **A server→host secret frame.** `HostStoreSecretFrame` (`omnigent/host/frames.py:642-709`) already
   ships for the Web UI's harness-credential dialog — correlated request/result, redacted,
   server is an authorized pass-through that never persists. Precedent for the handoff.
4. **A refresh hook for keys that change mid-session.** `CredentialRewriteRule` carries an
   optional `secret_provider` callable, and `resolve_secret()` prefers it — called on
   **every swap** (`omnigent/inner/credential_proxy.py:86-118`, `omnigent/inner/egress/proxy.py:1298`). Databricks OAuth already
   uses it (`omnigent/inner/credential_proxy.py:468`).
5. **Operator-authoritative egress override.** `_SANDBOX_OVERRIDE_KEYS` already includes
   `egress_rules` (`omnigent/spec/safety.py:432-446`).

**One constraint:** the frame protocol is strict-major (`omnigent/server/routes/host_tunnel.py:66`). A new frame
kind is additive, but an old host will not understand it, so version skew must fail closed.

---

## 5. The shape of the answer

Four whole-shape options were weighed; the reasoning and the two rejections are in **§A**.
The result is a sequence, because the middle two options are the same data model with
different transports:

```
  0  principal + binding model    (schema only)      → §6 D1
  1  commit identity  ~50 lines   no key involved    → PR1
  2  the model + Settings > Git   request A's ask    → PR3, PR4
  3  delivery hardening           git + gh together  → PR6
  4  broker slot present, unused  additive later     → PR3
```

**On restarting #2758:** directionally right, and the field requests vindicate its
architecture. Yes *with a scope cut*: split the model + UI from the sealed delivery, rekey to
principal ids, and settle tunnel trust before rebuilding any crypto.

---

## 6. The eight decisions

D1–D7 settled 2026-07-31. D1 was **revised** and D8 **added** on 2026-08-04 after review;
both superseded designs are in §B.

| | Question | Decision |
|---|---|---|
| **D1** | What identity does a session carry? | **Immutable execution principal + an authorized credential set**, one per canonical host |
| **D5** | Does MCP ride along in v1? | **Target discriminator now**, git-first build |
| **D8** | Who holds the secret, and how does it reach the runner? | **External reference + runner-pulled lease.** Encrypted DB is a fallback provider |
| **D2** | Is the tunnel trusted with secrets? | **Require TLS when secrets flow**, loopback exempt |
| **D3** | Must delivery turn on egress filtering? | **Auto-derive the rule**, operator can widen |
| **D7** | Can the pre-clone carry a key? | **No — never.** The runner clones credentialed repos |
| **D4** | Reject clones from unregistered hosts? | **Only credentialed clones** need a host record |
| **D6** | What happens on revoke? | **Re-resolve per swap**, bounded TTL |

### D1 + D5 — the model

Two tables, not one. The first says *what a principal is allowed to use*; the second says
*what this session actually runs as*. The first draft had only the first, which is the gap
review caught: a table alone cannot answer "which key?" when a principal has two.

```
  git_credentials  (SqlPrincipalCredential)      "what P may use"
  ┌────────────────────┬──────────────────────────────────────────────┐
  │ workspace_id    PK │ tenant partition (0 = default)               │
  │ id              PK │ Uuid16 — THE opaque credential slot          │
  ├────────────────────┼──────────────────────────────────────────────┤
  │ principal_kind     │ int code: user=1 | agent=2          ← D1     │
  │ principal_id       │ "alice@acme.com" | "refactor-bot"            │
  ├────────────────────┼──────────────────────────────────────────────┤
  │ target_kind        │ int code: git_host=1 | mcp_server=2  ← D5    │
  │ target_id          │ "acme-forgejo" | (later) "github-mcp"        │
  ├────────────────────┼──────────────────────────────────────────────┤
  │ label              │ user-chosen: "work" / "personal"             │
  │ secret_ref         │ EXTERNAL reference, not a secret     ← D8    │
  │ created_at         │                                              │
  │ updated_at         │                                              │
  └────────────────────┴──────────────────────────────────────────────┘

  UNIQUE(workspace_id, principal_kind, principal_id, target_kind, target_id, label)


  session_credentials  (SqlSessionCredentialBinding)   "what THIS session runs as"
  ┌────────────────────┬──────────────────────────────────────────────┐
  │ workspace_id    PK │                                              │
  │ session_id      PK │                                              │
  │ credential_id   PK │ → git_credentials.id  (no FK; portability)   │
  ├────────────────────┼──────────────────────────────────────────────┤
  │ principal_kind     │ the EXECUTION principal. Immutable after     │
  │ principal_id       │ create — never recomputed from the caller.   │
  │ canonical_host     │ "git.acme.com" — the routing key             │
  │ launch_generation  │ int, bumped per launch/relaunch              │
  └────────────────────┴──────────────────────────────────────────────┘

  UNIQUE(workspace_id, session_id, canonical_host)   ◄── THE routing invariant
```

Three properties fall out of that second `UNIQUE`:

```
  one session, many keys      ✓  different hosts → different rows
  two keys, same host         ✗  rejected at bind time as ambiguous
  "who am I?" at push time    ✓  read the row; never ask the caller
```

**Why the principal must be immutable.** If delivery recomputes identity from the current
caller or the session owner, an EDIT grantee directing the agent silently pushes as somebody
else. Writing the principal once at create and reading it thereafter is what makes §8's
shared-session question a *policy* choice about what to write, rather than an accident of who
happened to call.

**Why reject two keys for one host instead of adding a selector.** A selector needs a
grammar — by repo path, by command profile — and `gh` needs its own *active account* pointer
on top. That is a feature, not a constraint, and it can be added later without a migration
because the routing key is already `canonical_host`. Rejecting at bind time keeps v1
deterministic and fails at configuration time rather than at push time.

**Why an opaque principal id.** `SqlAgent` (`omnigent/db/db_models.py:246-300`) has **no
owner field** — an agent is a template, not an actor. But `SqlScheduledTask.user_id`
(`omnigent/db/db_models.py:1381-1385`) already documents "the user the spawned session's
`LEVEL_OWNER` grant is assigned to," resolving to `"local"` in OSS
(`omnigent/server/auth.py:44`). A non-human trigger acting as a named principal is **already
a pattern here**.

**Why MCP is a discriminator, not a schema.** `MCPServerConfig`
(`omnigent/spec/types.py:868-940`) carries auth as **static YAML** —
`headers: {Authorization: "Bearer tok"}`. That is the identical defect to `GIT_TOKEN`, one
layer up. `databricks_profile` in that same class is the pattern to copy: a *reference*
resolved at runtime. Adding `target_kind` now costs nearly nothing; retrofitting it costs a
migration.

Portability rules observed throughout: no foreign keys, no partial indexes, `Uuid16` ids,
enums as int codes.

### D8 — external reference, runner-pulled lease

Two facts about the tree decided this.

**Fact one: there is one more process boundary than the first draft admitted.**
`HostStoreSecretFrame` reaches the **host process**
(`_handle_store_secret`, `omnigent/host/connect.py:1657`), which then spawns the runner as a
*separate subprocess* (`subprocess.Popen`, `omnigent/host/connect.py:1178`) with a filtered
environment (`_build_runner_env`, `omnigent/host/connect.py:553`). "Server → runner" was
never one hop.

**Fact two: `resolve_secret()` is synchronous.** It is called inline on every swap
(`omnigent/inner/credential_proxy.py:100-117`, from
`omnigent/inner/egress/proxy.py:1298`). A server round-trip inside it blocks the proxy —
possibly on the tunnel it is serving.

So: don't push the secret down a hop it doesn't need to travel. Have the runner pull it.

```
  ✗ REJECTED (see §B)              ✓ CHOSEN
  server                            server authorizes bindings
    │ secret in frame                 │ (no secret moves yet)
    ▼                                 ▼
  host process ← extra custodian    host launches runner
    │ secret in env / on disk         │ binding token ONLY
    ▼                                 ▼
  runner                            runner authenticates, THEN pulls its
                                    own launch manifest
                                      │  request carries no caller-supplied
                                      │  principal or host — the server
                                      │  derives session + launch_generation
                                      │  from the runner binding
                                      ▼
                                    leases held in parent memory
                                      ├─ install proxy rules
                                      ├─ deferred clone (D7)
                                      └─ THEN start the agent
```

**The two-clock rule for refresh.** An async lease manager refreshes *before* expiry;
`secret_provider` only ever reads the current cached lease and never does network I/O. An
expired lease with no successful refresh **fails closed**. This keeps D6's revocation
semantics while respecting the synchronous signature the type already has.

**Custody, strongest to weakest:**

| Mode | Server sees plaintext? | Note |
|---|---|---|
| Runner-direct via workload identity (Vault/OpenBao JWT, cloud federation) | **no** | the strongest mode; nothing new in tree today |
| Response wrapping — server passes a single-use short-TTL wrapping token | no, but | tamper-*evident*, not proof against a malicious server |
| Dynamic minting — GitHub App installation tokens, 1h, repo/permission-scoped | short-lived only | App key stays in the external store |
| Encrypted DB column (Fernet, key list) | **yes** | explicit **fallback** provider, not the contract |

`Fernet` is not novel here — `EncryptedTokenStore`
(`integrations/slack/src/omnigent_slack/tokens.py:67`) already does it. But that precedent is
a Slack integration, not the main server's trust boundary, which is exactly why it makes a
poor *default* for this.

**CSI / file mounts are a Kubernetes integration, not the abstraction.** They are not
portable across the nine providers and may be visible to other processes in the box.

**The ingestion problem this exposes.** If Settings > Git POSTs a PAT, the server sees
plaintext no matter how it is stored afterward. A genuinely server-blind mode must either
accept an already-provisioned external reference or use a one-time scoped direct-write flow.
This is now an explicit obligation on PR4, which previously did not mention it.

**Obligations:** the manifest request must be unforgeable — derived from the runner binding,
never from request fields; leases live in parent memory only, never the runner's environment
or disk; `launch_generation` invalidates a stale lease across relaunch; and see §8 item 5,
because runner-direct retrieval needs a defined answer in header/proxy auth mode.

### D2 — require TLS when secrets flow

**The finding that decided it.** #2758 encrypted the credential *inside* the frame
(X25519+HKDF+ChaCha20-Poly1305) so confidentiality would not depend on deployment TLS. But
on `ws://` the tunnel's own auth bearer crosses **in the clear, before any frame exists**
(`omnigent/host/connect.py:942`, `:2351`, `:2271`). Sealing a git PAT inside a frame protects the PAT
while leaving the credential that owns the whole host exposed.

**The rule.** If a secret would cross the tunnel, require `wss://`. Loopback
(`http://127.0.0.1:8123`) is exempt — that is the normal local dev default (`omnigent/cli.py:2169`).

**What it buys:** deletes ~180 lines of hand-rolled crypto from this feature, and closes the
same pre-existing gap for `binding_token` and harness API keys. The single biggest
simplification available.

**Obligations:** refuse loudly at launch, naming the config key to fix; apply the rule to
*all* secret-bearing frames, not just the new one.

### D3 — auto-derive the egress rule

**The trap this closes.** `credential_proxy` requires `egress_rules` to be non-empty
(`omnigent/spec/parser.py:984`) but **cannot** require a rule that matches the credential's host — the
host is resolved per session at launch, long after parse time. So:

```
   egress_rules: ["* pypi.org/**"]     ← non-empty, so the parser is happy
   credential:   github.com            ← resolved later; no rule matches
        │
        ▼
   push goes out TOKENLESS → a 401 from GitHub
   Looks configured. Isn't. Nothing warned.
```

**The rule.** Derive `"* <credential-host>/**"` from the credential itself and merge it in.
An operator may widen it; the operator's rules win. This is cheap because
`_SANDBOX_OVERRIDE_KEYS` (`omnigent/spec/safety.py:432-446`) already makes operator egress override a
shipped pattern.

**Why not decouple delivery from egress:** the same proxy that enforces rules is what 403s a
decoy replayed at the wrong host. Turning off one turns off the other.

**Obligations:** if derivation is impossible (malformed host, non-HTTPS), refuse the launch
with a distinct error — silent-tokenless must be unreachable; show the derived rule in the
effective spec; `sandbox.type: none` still refuses, because derivation supplies the *rule*,
not the *fence*.

### D7 — the pre-clone never carries a key

**What the pre-clone is.** A managed create does not ask for an empty box; it asks for a box
with a repo in it (`RepoWorkspace`, `omnigent/server/managed_hosts.py:431`). So the repo must land on disk
before the agent has a working directory:

```
   1. provision the box
   2. mkdir <workspace>
   3. git clone                    ◄── THE PRE-CLONE
   4. start `omnigent host`
   5. host dials the server, gets a runner
   6. runner parent starts the egress proxy   ◄── the swap lives HERE
   7. runner starts the agent (fenced child)
```

Step 3 is three steps ahead of step 6. No proxy, no parent holding a secret, no fence — the
whole mechanism is **structurally unavailable**, on every provider.

**Decision.** The pre-clone is a **public-repo and cache optimization only**. A repo needing
a per-principal key is cloned by the runner through the proxy, on the same path as push and
`gh`. No key ever reaches a pre-host step, on any provider.

**The decisive reason, of three.** The pre-clone is where **authorization** is missing, not
just secrecy. Today it authenticates with a token belonging to nobody in particular:

```
  session principal:  refactor-bot        (should reach: acme/api)
  workspace:          github.com/acme/payroll   (private)

  pre-clone uses the deployment-wide GIT_TOKEN (org-scoped)
    → clone SUCCEEDS
    → the agent has payroll on disk, having presented no key of its own
```

Per-principal keys on push and `gh` fix nothing here — the *read* already happened at step 3
under someone else's authority. Routing credentialed clones through the principal's own key
makes **the forge the authorizer**: no key for that host, no clone; a key without the grant
gets a 404. Authorization for free, with no grant table of our own.

A k8s-only fast path was considered and rejected — §B5.

**The rule.**

```
  public repo                       → pre-clone, unchanged, all 9 providers
  covered by the deployment token   → pre-clone, unchanged (operator's explicit choice)
  needs a principal key             → SKIP the pre-clone; the runner clones after step 6
```

**The cost, stated plainly.** A private-repo session reaches a ready checkout **later** —
seconds to tens of seconds on a big monorepo. That is a real regression in felt latency,
accepted because the alternative is a PAT in a vendor's log or a second mechanism that
removes none of the first.

**Obligations:** the deferred clone is a first-class step with its own progress stage
(`on_stage("cloning")`, `omnigent/onboarding/sandboxes/base.py:878`) and its own error, not a hidden `git clone` in the
agent's first turn; the egress rule must be in force before it runs, since it is now the
*first* credentialed operation; the deployment-wide `GIT_TOKEN` pre-clone **stays** and is
documented as a shared-identity read, so nothing breaks; cache/mirror overrides
(`omnigent/onboarding/sandboxes/base.py:874-875`) never needed a key and are untouched.

### D4 — strict topology only for credentialed clones

**What topology is.** An operator-owned, entirely non-secret record saying a forge exists and
how to reach it:

```yaml
git_hosts:
  - id: acme-forgejo
    provider: forgejo
    web_host: git.acme.com
    credential_source: env:ACME_FORGEJO_TOKEN     # a REFERENCE, not a secret
```

Operators own it (a user who could invent a record could point an existing key at a host they
control). D4 is only about what happens when there is **no** record.

**Today** `parse_repo_workspace` (`omnigent/server/managed_hosts.py:541`) checks **shape only**
— scheme, host present, path present, legal branch name. It never looks at *which* host, so a
public gitlab.com clone works right now. #2708 would have 422'd any unregistered non-github
host (§B7).

**Why D7 settles it.** D7 split cloning into two populations:

```
  needs no key  ──►  consults topology at NO point. Nothing to look up.
  needs a key   ──►  REQUIRES a record — it is what mints the canonical host, the
                     target_id the UNIQUE constraint selects on, and the egress rule.
```

So strictness on the credentialed path is **not a policy choice but a structural fact**: you
cannot resolve a key for a host you have no record of. And strictness on the uncredentialed
path enforces a rule guarding a path the clone was never on. **The breaking change
disappears** — #2708's security property survives with no user-visible regression, and no
`github.com` carve-out (that carve-out was the tell that the rule was cutting in the wrong
place).

**The honest counter-argument.** An operator may want the *URL* controlled so an agent cannot
clone arbitrary code off the internet. That is real — but it is an **egress** concern with an
existing mechanism (D3, plus `_SANDBOX_OVERRIDE_KEYS`). Bundling it into the credential
registry means an operator who wants per-principal keys and no URL limits gets the limits
anyway.

**Obligations:** the refusal names the missing record *and* the `git_hosts:` block to add;
refuse before any durable row exists; **re-check at launch**, because operator config drifts
and a record present at create may be gone at relaunch; no `github.com` special case.

### D6 — revocation re-resolves per swap

**A correction that changed the answer.** An earlier pass recorded the runtime as having no
post-spawn refresh channel. Wrong. `CredentialRewriteRule` has two secret paths:

```
  resolve_secret()                    called on EVERY swap — proxy.py:1298
  ┌──────────────────────────────────────────────────────────┐
  │  if secret_provider is not None:                          │
  │      return secret_provider()   ◄── re-resolved per request│
  │  if real_secret is not None:                              │
  │      return real_secret         ◄── frozen at start        │
  └──────────────────────────────────────────────────────────┘
                                        credential_proxy.py:86-118
```

The docstring names our case: *"for sources whose secret expires and must be refreshed for
long sessions."* Databricks OAuth already ships against it.

**Decision.** A git rule's `secret_provider` reads a lease that an async manager keeps fresh
(D8 — the provider itself must not do I/O). A deleted or rotated key stops being attached
within the lease TTL. **The session is not killed.** Kill-and-relaunch was rejected on
proportion — §B6.

**Two costs, stated.** (1) **The TTL is the promise** and must be documented as a number; 60
seconds means a 60-second window — far better than "until the session ends," but not immediate.
(2) Refusal must be legible: the proxy's current failure path is `_send_bad_gateway`
(`omnigent/inner/egress/proxy.py:1315`), and `502 Bad Gateway` on `git push` explains nothing.

**What this does not promise.** Nothing here revokes the key **at the forge**. If a token was
already exfiltrated, no TTL helps. Deleting the PAT at GitHub stays the authoritative
emergency stop.

**Obligations:** kill stays available as an operator action; a provider failure **fails
closed** — never a stale value past TTL, never a fallback to the shared `GIT_TOKEN`; state the
TTL in docs; do **not** extend this to `env`/`file`/`command` sources, which are operator-owned
and have no row to consult.

---

## 6a. What the decisions did to the shape of the work

Four of eight came out **cheaper** than the options they replaced, for one consistent reason:
the careful plumbing already in tree (§4) is reusable, so most of this is a new data model
flowing through existing machinery.

- **D2** deletes ~180 lines of hand-rolled crypto and closes a pre-existing gap for other
  secrets at the same time.
- **D3** reuses a shipped operator-override pattern.
- **D7** needs **zero provider code** across all nine providers.
- **D4** removes the breaking change entirely.
- **D6** uses an existing extension point, so revocation costs a frame, not a subsystem.
- **D8** *removes* a hop rather than specifying one, and drops the encryption-key management
  that `token_ciphertext` would have made mandatory.

**Two ordering lessons.** **D7 decided D4** — answering the pre-clone question before the
topology question was accidental, and it is the whole reason #2708's breaking change
evaporated. And **D8 could not be decided before D1**: until the session had a binding to
name, "retrieve the exact bound secret" had no referent.

**Three costs deliberately accepted:**

1. **Private-repo checkouts are ready later** (D7).
2. **Revocation is TTL-bounded, not immediate** (D6). Document the number.
3. **Two keys for one host is rejected in v1** (D1) — deferred behind a future selector
   rather than guessed at.

**Two obligations that cut across everything:**

- **Fail closed; never fall back to the shared `GIT_TOKEN`.** D3, D4 and D6 each arrive here
  independently. A key request that cannot be satisfied must fail — resolving to somebody
  else's token is the exact failure mode this design exists to remove.
- **Every refusal names its cause.** A missing host record, an underivable egress rule, and a
  revoked key are three different problems that would otherwise all look like one opaque auth
  failure.

**What each field request gets.** Request A: Settings > Git with a per-user PAT at step 2; its
MCP question answered at step 3 by the same hop; its OIDC question answered honestly as "PAT
now, GitHub App later"; and its dedicated-host-per-user workaround becomes unnecessary — worth
telling them, since it is a large detour to avoid. Request B: per-agent keys at step 2 *because*
D1 went the right way, `gh` at step 3, and the broker recorded as the destination — the honest
framing being "you get per-agent `gh` keys well before a broker, and the broker adds auditing
on top."

---

## 7. The PRs

Nine PRs in four waves. Each is independently reviewable, each leaves `main` working, and
none needs the next one to be useful. Sizes are implementation lines, excluding tests.

```
 WAVE 1 — no credentials involved. Land in any order, immediately.
   PR1  commit identity            ~50    fixes problem 4
   PR2  require TLS for secrets    ~80    fixes a pre-existing hole (D2)

 WAVE 2 — the model. PR3 gates the rest.
   PR3  2 tables + store + migration ~350 D1, D5, D8
   PR4  routes + Settings > Git    ~450   ← request A's actual ask
   PR5  host topology              ~200   D4

 WAVE 3 — the pipe. This is the load-bearing wave.
   PR6  manifest + lease manager   ~450   ★ the missing pipe (D8)
   PR7  egress rule derivation     ~120   D3
   PR8  deferred credentialed clone ~200  D7

 WAVE 4 — after the pipe works.
   PR9  gh + MCP on the same hop   ~150   fixes problem 5
```

```
  PR1 ─┐
  PR2 ─┤ (independent)
       │
  PR3 ─┴─► PR4 ─┐
       └─► PR5 ─┤
                ├─► PR6 ─┬─► PR7 ─► PR8
                          └─► PR9
```

### Wave 1 — ship this week

**PR1 · Commit identity.** Set `GIT_AUTHOR_*` / `GIT_COMMITTER_*` in the runner environment
from the session principal (`_build_runner_env`, `omnigent/host/connect.py:542`). Verified absent today:
no `GIT_AUTHOR` anywhere in the tree, so commits get the image default. **No credential
involved at all** — this is why it goes first. Unblocks signed commits.
*Test:* a commit made by an agent carries the expected author and committer.

**PR2 · Require TLS when secrets cross the tunnel.** Refuse to send a secret-bearing frame
over `ws://`, loopback exempt (`omnigent/host/connect.py:942`). Applies to `HostStoreSecretFrame` and
`binding_token` too, not just future frames. **Fixes a hole that exists today**, and is a
prerequisite for PR6 shipping without hand-rolled crypto.
*Test:* a non-loopback `ws://` deployment refuses with an error naming the config key; `wss://`
and loopback both pass.

### Wave 2 — the model

**PR3 · The two tables.** `SqlPrincipalCredential` **and**
`SqlSessionCredentialBinding` exactly as specified in §6 D1, plus a store (copy the
`scheduled_task_store/` shape) and a migration. `secret_ref` holds an external reference;
the encrypted-column provider is a **named fallback**, not the schema's assumption (D8).
Portability: no FKs, no partial indexes, `Uuid16`, int enum codes. **Pick a fresh migration
id** — #2758's `z8a2b3c4d5e6` collides with
`z8a2b3c4d5e6_widen_conversation_items_pk_with_created_at.py` on `main`.
*Test:* the per-host UNIQUE rejects a second credential for the same canonical host on one
session; a session binds two credentials for two different hosts successfully; the execution
principal cannot be updated after create; `launch_generation` bumps on relaunch; a `broker:`
and an encrypted-fallback `secret_ref` both round-trip; upgrade + downgrade run clean.

**PR4 · Routes, the UI, and ingestion.** `/v1/git-credentials` CRUD plus **Settings > Git**.
Routes **derive** principal and workspace from the authenticated caller and **reject** any
client-supplied authority. Never return a token, only metadata. This is the PR request A
actually asked for — shipping the model without it is why #2758 read as infrastructure with
no payoff.

**The ingestion decision belongs here** (D8): accepting a pasted PAT means the server sees
plaintext, whatever the storage does afterward. Offer *both* — an external-reference path
(server-blind, the documented default) and a pasted-PAT path (explicitly labelled in the UI as
server-custodied, using the fallback provider).
*Test:* a caller cannot write a row for another principal; GET never leaks ciphertext or
plaintext; the external-reference path never puts a secret through the API; the pasted path
warns in the UI and records which provider stored it.

**PR5 · Host topology.** The `git_hosts:` config parser (typed, fail-closed, mtime-refreshed)
and resolution by exact canonical host. **Only enforced for credentialed clones** (D4), so
this PR is not a breaking change. Refusals name the missing record and the config block.
*Test:* a public unregistered-host clone still works; a credentialed one refuses with a
useful message; a record removed between create and relaunch refuses at launch.

### Wave 3 — the pipe

**PR6 · The launch manifest and the lease manager.** The load-bearing PR, and the one review
reshaped most (D8). **No secret crosses the host process.** The runner is launched with its
binding token, authenticates, and then pulls its own manifest of credential leases:

```
  1  server writes session bindings          (PR3; no secret moves)
  2  host launches runner — binding token only
  3  runner authenticates on its tunnel
  4  runner GETs its launch manifest
       server derives session + launch_generation FROM THE BINDING,
       never from a request field
  5  leases → parent memory → CredentialRewriteRule
  6  egress rules installed (PR7)
  7  deferred clone (PR8)
  8  agent starts — after all of the above
```

Two components, deliberately split by the synchronous-`resolve_secret()` constraint:

- **An async lease manager** in the runner parent. Refreshes each lease *before* expiry.
  Owns all network I/O.
- **`secret_provider`, sync and I/O-free.** Reads the current cached lease and nothing else.
  An expired lease with no successful refresh **fails closed** — never a stale value, never a
  fallback to the shared `GIT_TOKEN`. This satisfies D6 without blocking the proxy.

Strict-major protocol (`omnigent/server/routes/host_tunnel.py:66`) means an old host must
**fail closed**, not silently skip the credential.
*Test:* an old-protocol host fails closed with a clear error; a forged manifest request naming
another session's principal or host is refused; a stale `launch_generation` is refused after
relaunch; the secret appears in no log, span, error body, runner env, or on disk; a rotated
key is picked up within the TTL; `secret_provider` performs **no** network I/O under test (a
blocked-socket assertion); an expired lease with a failed refresh fails closed rather than
falling back.

**PR7 · Derive the egress rule.** Auto-derive `"* <host>/**"` from the credential and merge,
operator rules winning (D3). Refuse the launch if derivation is impossible. Surface the derived
rule in the effective spec.
*Test:* the silent-tokenless case from §6 D3 now refuses instead of 401ing; an operator's wider
rule survives the merge; `sandbox.type: none` still refuses.

**PR8 · Deferred credentialed clone.** Skip the pre-clone when the repo needs a principal key;
clone from the runner through the proxy instead, as a first-class step with its own
`on_stage("cloning")` progress and its own error. Public and deployment-token pre-clones stay
exactly as they are (D7).

Two mechanics decide whether this actually works, and both need to be explicit rather than left
to git's defaults:

- **Displace the baked helper, don't race it.** `deploy/docker/Dockerfile:233` installs a `--system`
  host-blind helper that answers *any* host from `GIT_TOKEN`. A credentialed clone adds a second
  willing helper, and resolution order then decides which token leaves the box. Reset the
  inherited chain first so the per-principal helper wins outright:
  `git -c credential.helper= -c credential.helper=<ours> ...`. The empty assignment clears the
  chain; ours is then the only entry. Without it, the ambient token can answer for a host the
  principal was never granted — the exact leak D7 exists to prevent.
- **Persist the helper into the checkout.** Pass both `-c` options to `clone` itself, not to a
  wrapping shell. Git uses them for the clone's own fetch **and** writes them into the new
  repo's local config, so the agent's later `fetch` / `push` in that workspace resolve the same
  principal credential with no re-teaching and no ambient fallback. This is what closes §8
  item 2's other half for a fresh clone.

*Test:* a private repo clones with the principal's key and **not** the shared one; with
`GIT_TOKEN` also set in the environment, the ambient token is **not** offered; a later `push`
in the resulting checkout still authenticates as the principal without re-injection; a key with
no grant surfaces the forge's 404, not an empty workspace; a public repo still pre-clones.

### Wave 4

**PR9 · `gh` and MCP on the same hop.** Point the existing `gh_basic` decoy mechanism at the
delivered credential, and add `target_kind = mcp_server` resolution for `MCPServerConfig` —
copying the `databricks_profile` reference pattern instead of static YAML. Note macOS is
excluded for `gh` (§1, item 3).

Give `gh` a **per-session `GH_CONFIG_DIR`** rather than letting it use the default. The point is
*not* secrecy — `gh` holds only a synthetic `oa_cred_*` decoy
(`omnigent/inner/credential_proxy.py:52`), so isolating decoys buys little. It is that the config dir is the
only place `gh` can be told about a non-GitHub host, which is what an Enterprise target needs,
and a per-session dir keeps no `gh` state across sessions.

**Do not route git through `gh auth git-credential`** — §B8.

*Test:* `gh` authenticates with only a decoy in its environment; a decoy replayed at another
host gets a 403; an MCP server resolves its header at runtime.

### What is deliberately not here

- **A full audited broker.** D8 adopts the broker's *custody* shape (external references,
  runner-pulled leases) but not its auditing and metering surface, which is what request A
  asked for beyond delivery. That sits on top of this model, additively.
- **SSH and GPG sockets.** Real sockets in §0's layer 4, but no field request has asked yet.
- **OIDC-derived tokens.** Needs a GitHub App. PAT-first is the honest v1.
- **Removing the deployment-wide `GIT_TOKEN`.** See §8 item 3 — an operator sequencing problem,
  and it needs docs before any code.

### Order of operations

PR1 and PR2 can go up now. PR3 is the gate for everything after it, so it is worth reviewing
carefully — it is the one thing here that is expensive to change after ship, and review has
already reshaped it once. PR6 is where the genuine risk lives; everything before it is additive
and everything after it is a consumer. **PR6 should not start before §8 item 5 is answered** —
header-mode auth decides whether the runner can pull its own manifest at all.

---

## 8. Still open

Five questions. None blocks starting; all need answers before ship.

1. **Which principal does a shared session run as?** D1 now says the answer is *written once
   at create and never recomputed* — but not **what** to write. #2758 chose owner-authority
   plus warn-and-allow: an EDIT grantee can direct the agent to push as the owner. Deliberate
   — a hard gate leaves the agent unable to commit — but it needs maintainer ratification, not
   a PR footnote. D1 turned this from a silent accident into an explicit policy choice; the
   choice itself is still open.
2. **Wake and relaunch.** `resume_managed_host` passes `repo_url=None`
   (`omnigent/server/managed_hosts.py:2565`), so a resumed box never re-clones but still needs
   fetch/push auth. D4's re-check-at-launch, D6's `secret_provider` and D8's
   `launch_generation` together cover it. Needs an explicit test: **a session resumed after
   its key was rotated must use the new token, and a lease from the previous generation must
   be refused.**
3. **Migrating off the ambient token.** The sharpest one, and none of D1–D8 solves it.
   Kubernetes projects the harness Secret into the *host process* container via `envFrom`, and
   there is no per-key filter — a running process's environment cannot be scrubbed after exec.
   So the deployment-wide `GIT_TOKEN` stays in the Pod until an operator removes it from the
   Secret, and at that moment the legacy path stops working for everything still depending on
   it. D7 keeps that path deliberately alive, which makes this a **sequencing** problem for
   operators: stand up per-principal keys, verify them, *then* remove the shared token. That
   sequence needs writing down as operator documentation, not inferring.
4. **Is `gh` actually in the image?** I cannot find it installed anywhere. `deploy/docker/Dockerfile:213`
   installs `git tmux procps lsof bubblewrap curl ca-certificates unzip iproute2 nftables`, and
   the npm globals at `:251` are `@anthropic-ai/claude-code` and `@openai/codex`. Neither
   `github-cli` nor `cli.github.com` appears in the repo. Yet `gh_basic` exists specifically to
   wire `gh`, and carries a detailed macOS/Go TLS rejection path (`omnigent/spec/parser.py:1763-1774`) written
   as though exercised. Either `gh` is expected from a user-supplied image, or that preset has
   never run end-to-end on a shipped image. **Confirm before PR9 leans on it** — if `gh` is
   BYO-image, PR9 owes a clear failure when the binary is absent rather than a confusing
   credential error.
5. **Runner-direct retrieval in header/proxy auth mode.** D8 has the runner authenticate and
   then pull its manifest. That leans on `mint_runner_token`, which returns `None` in `header`
   mode — identity there is asserted by the upstream proxy and cannot be minted server-side
   (`omnigent/server/auth.py:463-478`), and `omnigent/server/routes/runner_tunnel.py:332`
   already raises `INVALID_INPUT` for exactly this case. Runner-direct works in `accounts` and
   `oidc`. Header mode needs a decision: restrict the feature there, accept a weaker
   host-mediated hop with the trade-off documented, or define a separate runner credential.
   **This is the one open question D8 depends on.**

---

## A. Rejected whole-shape alternatives

| | Shape | Fixes | Why rejected |
|---|---|---|---|
| **A** | Teach the baked helper `GIT_TOKEN_<HOST>` (#2689) | 1 only | **Rejected.** Correct for the filed issue and nothing else: no person dimension, key stays ambient, and it does nothing for `gh` or MCP because it only teaches the *git helper* about hosts. Its env-var naming would become a compat surface carried forever. |
| **B** | Per-principal keys in the DB, delivered through the existing `GIT_TOKEN` env channel | 1, 2 | **Superseded, not wrong.** The right user-facing model with the wrong transport — it concedes the key is visible in the box. Kept as the *model* half of the plan; the transport is replaced by C/D8. |
| **C** | B, plus deliver to the trusted parent and swap in the proxy (#2758) | 1, 2, 3 | **Adopted with a scope cut**, then further revised by D8, which removes the host hop C assumed. |
| **D** | Broker: the key never leaves the server | 1,2,3,5 + audit | **Adopted as the destination.** D8 makes it the *default* rather than a someday: external references and runner-pulled leases are the broker shape, arrived at from the custody argument instead of scheduled. |

Also rejected at whole-shape level: **giving every user a dedicated long-lived host** (request
A's own stated workaround). It abandons managed sandboxes to avoid a missing data model, and
costs an operator a box per user forever.

---

## B. Superseded mechanism-level designs

Each of these was written into an earlier draft of this doc and replaced after review. They are
recorded because a future reader will otherwise propose them again — they are all locally
reasonable.

### B1. `token_ciphertext` as the storage contract (was D1)

**Was:** the credential row held a Fernet-encrypted PAT, key list for rotation.

**Rejected because** it makes the Omnigent server the permanent secret custodian: it must
decrypt every credential, so at-rest encryption improves the database's posture while leaving
the server's unchanged. It also mandates encryption-key management we would then own forever.
Replaced by D8 — external reference plus runner-side lease, with the encrypted column retained
as an explicitly-named *fallback provider* so small deployments are not forced to stand up
Vault. `Fernet` itself was never the problem, and the precedent
(`integrations/slack/src/omnigent_slack/tokens.py:67`) is real.

### B2. Credential slot selected by `label` alone (was D1)

**Was:** `UNIQUE(..., label)` on the credential table, and PR6 resolving "principal P's key for
host H."

**Rejected because it was self-inconsistent.** The constraint permits
`("alice", github.com, "work")` and `("alice", github.com, "personal")` to coexist, while the
resolver has no way to choose between them — a design that stores ambiguity and a consumer that
cannot tolerate it. Replaced by the session-binding table in D1, with per-canonical-host
uniqueness enforced at bind time.

An intermediate proposal — *one* credential per session — was also rejected: it would have
blocked the legitimate case of one sandbox using different keys for different forges.

### B3. Server → host → runner secret push (was PR6)

**Was:** a new secret-bearing frame modelled on `HostStoreSecretFrame`, described as
"server → runner delivery."

**Rejected because the topology was wrong.** That frame terminates at the host process
(`omnigent/host/connect.py:1657`), which spawns the runner as a separate subprocess
(`omnigent/host/connect.py:1178`). The design named one hop where there are two, leaving the
host process an unspecified secret custodian. Replaced by D8's runner-pulled manifest, which
*removes* the hop instead of specifying it.

### B4. `secret_provider` performing a server lookup (was D6)

**Was:** the `secret_provider` callable asks the server for the principal's current key,
short-TTL cached.

**Rejected because `resolve_secret()` is synchronous**
(`omnigent/inner/credential_proxy.py:100-117`) and called inline on every swap from
`omnigent/inner/egress/proxy.py:1298`. A server round-trip there blocks the egress proxy, and
in the worst case deadlocks against the tunnel it is serving. Replaced by D8's two-clock split:
async lease manager refreshes ahead of expiry, `secret_provider` reads cache only. D6's
revocation semantics survive intact — only the mechanism changed.

### B5. A k8s fast path for the credentialed pre-clone (was D7)

**Was:** add the token as a second key in the existing per-launch Secret — about a dozen lines,
lifecycle already written.

**Rejected** on three counts, kept here because it is the most tempting shortcut in the whole
design: it cannot generalize (the other eight providers reach the box only through
`run(sandbox_id, command: str)` with no environment parameter, so a key could only ride inside
a command string into a third-party SaaS request body and log); two paths means two sets of
bugs while removing none of the deferred path's work; and it addresses secrecy while leaving
the *authorization* gap — the pre-clone reads under a token belonging to nobody in particular.
Full reasoning in §6 D7.

### B6. Kill-and-relaunch on revocation (was D6)

**Was:** revoking a key terminates live sessions holding it (`HostStopRunnerFrame`,
`omnigent/host/frames.py:178`, already exists).

**Rejected on proportion**, not capability: it requires new bookkeeping to find affected
sessions, kills the agent mid-task with uncommitted work, and over-triggers when a session
holds two keys and only one is revoked. Re-resolution needs no lookup at all — the row is
simply gone. Kill remains available as a deliberate operator action.

### B7. Strict host topology for all clones (#2708)

**Was:** 422 any clone from an unregistered non-github host, with a `github.com` carve-out.

**Rejected because D7 made it unnecessary.** Once credentialed and uncredentialed clones are
separate populations, strictness on the credentialed path is structural — you cannot resolve a
key for a host you have no record of — and strictness on the uncredentialed path guards a path
the clone was never on. The carve-out was the tell that the rule was cutting in the wrong
place. #2708's security property survives with no breaking change and no 422 for public repos.

### B8. Routing git through `gh auth git-credential` (considered for PR9)

**Was:** use `gh` as git's credential helper, as a developer would locally.

**Rejected because it inverts the dependency.** It makes `gh` the credential authority and git
the consumer, which requires `gh` to hold a *real* token locally. Git authenticates by
swap-on-access with nothing in its environment
(`omnigent/spec/parser.py:1923-1931` binds the git host as `basic` with no `inject_env`); `gh`
gets a decoy only because it refuses to issue a request without seeing something. Routing git
through `gh` adds a dependency and undoes that property. The per-session `GH_CONFIG_DIR` from
the same idiom **was** adopted (PR9) — for Enterprise host config and session isolation, not
secrecy.
