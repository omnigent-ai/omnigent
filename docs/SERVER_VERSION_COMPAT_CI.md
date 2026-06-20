# Server-Version Backwards-Compatibility CI

**Status:** Spec (implemented — Phase 1)
**Branch:** `server-version-ci`

## 1. Goal

Catch backwards-incompatible **server** changes before release by running a
full test suite where the **server** is a different version from the
**client + runner + test code**.

Two directions, both holding the *server* as the lone odd-version component:

| Config | Server | Runner | Client + tests | Tests for |
|---|---|---|---|---|
| **1 — forward compat** | **OLD** | new | new (main) | a new client/runner still works against a not-yet-upgraded server |
| **2 — backward compat** | **NEW** | old | old (a released tag) | main's server didn't break things older clients rely on |

**Phase 1 (this doc, implemented): Config 1 only.** Run main's CI network
suites against a pinned-old server. **Config 2 is deferred** — see §7.

## 2. Why Config 2 is deferred (and why that's the cheap choice)

Config 2 means running an *old release's own test suite* against a new
server. An old release's tests can only redirect their server to "new" if
they already contain the redirect hook from §5. Releases cut **before** this
lands don't have it, so Config 2 against today's tags would require patching
each old checkout's fixtures — fragile and version-specific.

So: ship the hook now, let it ride out one release, and Config 2 lights up
for free using that (now hook-bearing) release as the "old" side. The hook
we add is therefore a **long-lived contract** — env-var names, fixture
behavior, and marker semantics below should not churn, because future
Config-2 runs depend on shipped releases honoring them.

## 3. The load-bearing constraint: only network suites can cross versions

Tests reach the server two structurally different ways, and only one can be
pointed at a different-version server:

| Style | Mechanism | Files | Crossable? |
|---|---|---|---|
| **In-process** | `create_app()` + `httpx.ASGITransport(app=app)` — server runs *inside* pytest | ~66 | ❌ No process boundary to put another version behind |
| **Network** | `live_server` fixture spawns `omnigent.cli server` (+ a runner) as a subprocess | ~32 | ✅ Yes |

(The two sets are disjoint — verified.) In-process suites (`tests/server/`,
much of `tests/frontends/`) test main's server by construction and give
**zero** compat signal, so the workflow runs the **network suites only**
(`tests/e2e/`, `tests/integration/`). `tests/e2e_ui/` is also network but
needs an npm build and is out of Phase-1 scope.

## 4. The clean structure (why this is simpler than it looks)

The **client lives in the test process** (pytest imports the SDK and calls
the server), and the **runner is spawned from whatever venv pytest runs in**
(`sys.executable`). Since Config 1 wants client *and* runner to be new (=
main = the test process), **only the server is ever redirected**; the runner
tracks pytest's venv automatically and needs no change.

```
┌─ test process = MAIN venv ──────────────┐      ┌─ server venv = OLD ─────────────┐
│ pytest + tests/      @ main             │      │ omnigent @ v0.1.1 (built from   │
│ omnigent_client      @ main  (client)   │ ───▶ │   the git tag into a venv)      │
│ runner: omnigent.runner._entry @ main   │ HTTP │   → omnigent.cli server          │
│   (spawned from sys.executable)         │      │                                  │
└──────────────────────────────────────────┘     └──────────────────────────────────┘
                  ▲ only the SERVER subprocess is redirected to the OLD venv
```

The runner↔server tunnel is therefore cross-version (new runner ↔ old
server). If its frame-protocol **major** ever bumps between the two versions
(`SUPPORTED_FRAME_PROTOCOL_MAJOR`, `omnigent/server/routes/runner_tunnel.py`),
the handshake is rejected (`4002`) and *all* network tests fail at the same
point — a real but blunt signal. (Config 2 will exercise the reverse:
old runner ↔ new server.)

## 5. How the server gets pinned (the mechanism)

### 5.1 Materialize the old server (workflow step)

Build the pinned version **from its git tag into an isolated venv** — works
for any ref regardless of publish state, and the server boots fine without
the web-ui bundle (the API/e2e paths don't need static assets):

```bash
git worktree add --detach "$SERVER_SRC" "$SERVER_VERSION"   # e.g. v0.1.1
uv venv --python 3.12 "$SERVER_ENV"
uv pip install --python "$SERVER_ENV/bin/python" -e "$SERVER_SRC"
"$SERVER_ENV/bin/omnigent" --version    # sanity
```

### 5.2 Redirect the server subprocess (test-harness change)

Two environment variables, read by a small shared helper
(`tests/_helpers/compat.py`):

- **`OMNIGENT_COMPAT_SERVER_PYTHON`** — interpreter the **server**
  subprocess runs under. When set, every server-spawn site (1) launches with
  this interpreter instead of `sys.executable`, (2) **drops the `_REPO_ROOT`
  PYTHONPATH prepend**, and (3) **runs the subprocess from a neutral CWD**.

  > Interpreter-swap alone is **not** sufficient — the worktree's `omnigent/`
  > can shadow the pinned install via **two** independent `sys.path` vectors,
  > both of which compat mode must neutralize:
  >
  > 1. **PYTHONPATH.** The fixtures inject `PYTHONPATH=<repo_root>` so the
  >    server imports the worktree source (intended: a branch tests its own
  >    server). Compat mode omits it.
  > 2. **CWD.** `python -m omnigent.cli` puts the CWD on `sys.path[0]`, and CI
  >    runs from the checkout root (which contains `omnigent/`). So even with
  >    PYTHONPATH dropped, an inherited CWD re-shadows. Compat mode runs the
  >    server from an empty temp dir.
  >
  > Verified directly: under the old venv's interpreter, `import omnigent`
  > resolves to the pinned source **only** when *both* are neutralized;
  > leaving either one pointed at the worktree loads main's code instead.

  The **runner is left untouched** — it keeps using `sys.executable` + the
  prepend, so it stays main (Config 1's "new runner").

- **`OMNIGENT_COMPAT_SERVER_VERSION`** — the version string the workflow
  pinned (e.g. `0.1.1`). Used by the skip logic (§6) as a backstop /
  cross-check, not to launch anything.

Spawn sites updated (all server, never runner):
`tests/_helpers/live_server.py:start_live_server`,
`tests/e2e/conftest.py` (the `live_server` and resume-server fixtures).

When neither env var is set → byte-for-byte today's behavior.

## 6. The `min_server_version` skip

A new feature whose tests would fail against an old server is marked:

```python
@pytest.mark.min_server_version("0.1.2")
async def test_uses_a_0_1_2_feature(live_server): ...
```

- **Marker** registered in `pyproject.toml`. Argument = the release the
  feature ships in. Convention: use the `X.Y.Z` main currently declares in
  `pyproject.toml` (drop the `.devN`) — see §6.1.
- **`server_version` fixture** (session-scoped, depends on `live_server`)
  resolves the running version. **Source of truth: `GET /api/version`.**
  `OMNIGENT_COMPAT_SERVER_VERSION` is a **backstop** (used only if
  `/api/version` can't be read) and a **cross-check**: if both are present
  and their release tuples disagree → **raise** (the tripwire for the
  PYTHONPATH-shadow regression — a shadowed old server would report main's
  version, not the pinned one).
- **Skip**: an autouse, marker-gated guard skips the test when
  `release_tuple(server) < release_tuple(required)`. Unmarked tests never
  resolve `server_version`, so non-server tests are unaffected. Lives in
  `tests/e2e/conftest.py`, re-exported into `tests/integration/conftest.py`.

Comparison is on the **PEP 440 release tuple** (`packaging.version`),
ignoring `.devN`/`rc` suffixes — see §6.1.

### 6.1 Fixing `/api/version` (the ordering bug)

`/api/version` reads `importlib.metadata.version("omnigent")` (correct) — but
main *declares* `0.1.0` in `pyproject.toml` while the latest tag is `0.1.1`,
so main reports a version **older than a shipped release**. Left unfixed, a
`min_server_version("0.1.2")` test would wrongly skip on main too. Fix, two
parts:

1. **Bump main ahead of releases.** main → `0.1.2.dev0` (the next release's
   dev; `0.1.2` isn't tagged yet). All three packages + their `==` cross-pins
   move together (`pyproject.toml`, `sdks/python-client`, `sdks/ui`), then
   `uv lock`. Release validation is **tag-gated** (`release-omnigent.yml`),
   so a `.dev0` on main is invisible to it. **Adopt the discipline of bumping
   main to the next `.dev0` right after each release cut** (ideally automated
   in the release workflow) so it never rots again.
2. **Compare on the release tuple.** `Version("0.1.2.dev0").release == (0,1,2)`,
   and `0.1.2.dev0 < 0.1.2` under full PEP 440 — so comparing the *release
   tuple* lets a `.devN` of X satisfy `min_server_version("X")`. Without this,
   main would skip its own just-landed features.

Result: normal CI (server = main = `0.1.2.dev0`) runs `0.1.2`-marked tests;
compat CI (server = `0.1.1`) skips them; both correct.

## 7. The GitHub Action (`.github/workflows/server-compat.yml`)

Modeled on `e2e.yml` / `integration.yml` (Databricks profile from secrets,
sharding). Sketch:

```yaml
name: Server Backwards-Compat
on:
  workflow_dispatch:
    inputs:
      server_version: { description: "Old server tag, e.g. v0.1.1", required: true }
  schedule:
    - cron: "0 11 * * *"   # nightly, after e2e/integration settle
jobs:
  compat:
    strategy: { fail-fast: false, matrix: { shard_id: [0,1,2,3] } }
    steps:
      - uses: actions/checkout@v4              # main: tests + client + runner
        with: { fetch-depth: 0 }               # full history+tags for the worktree
      - run: uv sync --extra all --extra dev
      - run: |                                  # §5.1 — build the OLD server
          git worktree add --detach "$RUNNER_TEMP/server-src" "${{ inputs.server_version }}"
          uv venv --python 3.12 "$RUNNER_TEMP/server-env"
          # All three packages editable from the old worktree so the old
          # server's ==<old> SDK cross-pins resolve without hitting an index.
          uv pip install --python "$RUNNER_TEMP/server-env/bin/python" \
            -e "$RUNNER_TEMP/server-src" \
            -e "$RUNNER_TEMP/server-src/sdks/python-client" \
            -e "$RUNNER_TEMP/server-src/sdks/ui"
          "$RUNNER_TEMP/server-env/bin/omnigent" --version
      - run: |  # ~/.databrickscfg from secrets — identical to e2e.yml
          ...
      - env:
          OMNIGENT_COMPAT_SERVER_PYTHON: ${{ env.RUNNER_TEMP }}/server-env/bin/python
          OMNIGENT_COMPAT_SERVER_VERSION: ${{ inputs.server_version }}   # leading v stripped
        run: |
          uv run pytest tests/e2e/ \
            --llm-api-key "$LLM_API_KEY" --profile default --harness databricks \
            -n 2 --dist=loadscope --shard-id=${{ matrix.shard_id }} --num-shards=4 \
            --timeout=180 --timeout-method=thread -m "not nightly" \
            --junitxml="artifacts/server-compat-${{ matrix.shard_id }}.xml" -v -r a
```

The implemented workflow (`.github/workflows/server-compat.yml`) has **two
jobs**: `compat-e2e` (sharded, `--harness databricks`, as above) and
`compat-integration` (per-harness matrix, `--integration --harness <h> --model
<m>`, mirroring `integration.yml`). They can't share one `pytest` invocation —
e2e runs the `databricks` harness while integration runs one of
`claude-sdk|codex|openai-agents` per leg. Network suites only. Trigger:
`workflow_dispatch` (a `server_version` input, default = latest non-rc tag) +
nightly; add to the PR gate only after burn-in.

## 8. Risks

1. **Launch-contract drift.** The fixture launches the old server with main's
   flags (`--port`, `--database-uri`, `--artifact-location`) and wires the
   runner via a token whose `runner_id` derivation must match across versions.
   If those changed, the suite breaks at **infra startup**, not in a test, and
   can't be `min_server_version`-skipped. That *is* a compat signal, just a
   blunt one. Keep launch flags to the long-stable set.
2. **Tunnel protocol major bump** (§4) — fails everything at the handshake.
3. **Marker curation** — adding a server feature means marking its tests;
   uncurated, a new-feature test is a false failure against the old server.

## 9. Non-goals (Phase 1)

- In-process suites (`tests/server/`, `tests/frontends/`) — can't cross a
  process boundary (§3).
- `tests/e2e_ui/` — network, but needs an npm build; deferred.
- Config 2 (old client/tests vs new server) — deferred until a hook-bearing
  release exists (§2).
- A general N×M version matrix — start with main-vs-one-old-version.

## 10. Implementation status

- [x] `tests/_helpers/compat.py` — server redirect (interpreter + PYTHONPATH
      drop + neutral CWD) and version resolution/skip helpers.
- [x] Redirect wired into all server spawn sites (runner untouched):
      `tests/_helpers/live_server.py` + both `tests/e2e/conftest.py` fixtures.
- [x] `min_server_version` marker registered; `server_version` fixture +
      autouse guard (cross-check fires once/session in compat mode);
      re-exported to integration.
- [x] Version bump to `0.1.2.dev0` across the 3 packages + `uv lock`.
- [x] `.github/workflows/server-compat.yml` (compat-e2e + compat-integration).
- [x] Unit test (`tests/test_server_compat.py`) for release-tuple comparison,
      version reconcile/backstop/tripwire, and redirect env/cwd shaping.
- [x] End-to-end redirect proof: built a real `v0.1.1` venv; verified the
      redirect loads `v0.1.1` code (`import omnigent.__file__` resolves to the
      old source only when both PYTHONPATH and CWD are neutralized) and that
      `/api/version` + the cross-check tripwire behave.
- [ ] Full e2e/integration suite run against a pinned-old server — requires
      Databricks creds (workflow-only; can't run in this dev sandbox).
- [ ] Config 2 (next release).
- [ ] Automate the post-release `.dev0` bump in `release-omnigent.yml` (follow-up).
