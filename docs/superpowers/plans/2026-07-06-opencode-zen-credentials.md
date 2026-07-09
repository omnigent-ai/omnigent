# OpenCode Zen API Key Credentials Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Omnigent store an OpenCode Zen API key (keychain secret `opencode-zen`) and deliver it to the opencode harness — locally and in sbx sandboxes — via the `OPENCODE_API_KEY` env var.

**Architecture:** One new resolver module (`omnigent/opencode_zen_credentials.py`) is the single source of truth for the key (env `OPENCODE_API_KEY` → `OMNIGENT_OPENCODE_API_KEY` → keychain). Consumers: the setup reporter (`onboarding/opencode_auth.py`), the runner spawn env (`runner/app.py`), the setup wizard drill-in (`cli.py`), and the sbx env passthrough (`onboarding/sandboxes/sbx.py`). Spec: `docs/superpowers/specs/2026-07-06-opencode-zen-credentials-design.md`.

**Tech Stack:** Python 3, pytest, existing `omnigent.onboarding.secrets` keychain store, `omnigent.env_credentials` helpers.

## Global Constraints

- The key is NEVER logged, never written to bridge dirs, never placed on a command line, and Omnigent NEVER writes opencode's own `auth.json`.
- Ambient env always wins over the keychain (the resolver checks env first; spawn-env stamping of an env-resolved value is a harmless no-op).
- Keychain read failures are treated as "no key" — session launch must never crash on a keyring error.
- Secret name is exactly `opencode-zen`; env var is exactly `OPENCODE_API_KEY`; the Omnigent-prefixed alias is `OMNIGENT_OPENCODE_API_KEY`.
- Run all commands from the repo root: `/home/jason/workspace/omnigent-worktrees/feature-opencode-go-credentials`.
- Follow repo comment rules (CLAUDE.md): short comments describing the scenario, never the PR/change history.
- Commit each task separately; `git commit` runs the pre-commit hook — fix anything it reports.

---

### Task 1: Zen key resolver module

**Files:**
- Create: `omnigent/opencode_zen_credentials.py`
- Test: `tests/test_opencode_zen_credentials.py`

**Interfaces:**
- Consumes: `omnigent.env_credentials.env_names_with_omnigent_prefix(name) -> tuple[str, ...]` (exists), `omnigent.onboarding.secrets.load_secret(name) -> str | None` (exists; imported lazily).
- Produces (later tasks rely on these exact names):
  - `OPENCODE_API_KEY_ENV_VAR: str = "OPENCODE_API_KEY"`
  - `OPENCODE_ZEN_SECRET_NAME: str = "opencode-zen"`
  - `KEYCHAIN_SOURCE: str = "keychain"`
  - `resolve_opencode_zen_key(environ: Mapping[str, str] | None = None) -> tuple[str, str] | None` — `(source, key)` where source is `"env:OPENCODE_API_KEY"`, `"env:OMNIGENT_OPENCODE_API_KEY"`, or `"keychain"`.
  - `zen_spawn_env(environ: Mapping[str, str] | None = None) -> dict[str, str]` — `{"OPENCODE_API_KEY": key}` or `{}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_opencode_zen_credentials.py`:

```python
"""Tests for OpenCode Zen API key resolution."""

from __future__ import annotations

import pytest

import omnigent.opencode_zen_credentials as zen


@pytest.fixture(autouse=True)
def _no_ambient(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear the Zen env vars and stub the keychain to empty."""
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)


def test_resolves_canonical_env_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-canonical")
    monkeypatch.setenv("OMNIGENT_OPENCODE_API_KEY", "sk-prefixed")
    assert zen.resolve_opencode_zen_key() == ("env:OPENCODE_API_KEY", "sk-canonical")


def test_falls_back_to_omnigent_prefixed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_OPENCODE_API_KEY", "sk-prefixed")
    assert zen.resolve_opencode_zen_key() == (
        "env:OMNIGENT_OPENCODE_API_KEY",
        "sk-prefixed",
    )


def test_blank_env_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "   ")
    assert zen.resolve_opencode_zen_key() is None


def test_env_value_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", " sk-pad \n")
    assert zen.resolve_opencode_zen_key() == ("env:OPENCODE_API_KEY", "sk-pad")


def test_falls_back_to_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    assert zen.resolve_opencode_zen_key() == ("keychain", "sk-vault")


def test_keychain_error_means_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_name: str) -> str | None:
        raise RuntimeError("keyring exploded")

    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", _boom)
    assert zen.resolve_opencode_zen_key() is None


def test_no_key_anywhere_is_none() -> None:
    assert zen.resolve_opencode_zen_key() is None


def test_explicit_environ_mapping_is_used() -> None:
    env = {"OPENCODE_API_KEY": "sk-explicit"}
    assert zen.resolve_opencode_zen_key(env) == ("env:OPENCODE_API_KEY", "sk-explicit")


def test_zen_spawn_env_stamps_resolved_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    assert zen.zen_spawn_env() == {"OPENCODE_API_KEY": "sk-vault"}


def test_zen_spawn_env_empty_when_unresolved() -> None:
    assert zen.zen_spawn_env() == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_opencode_zen_credentials.py -v`
Expected: FAIL at collection with `ModuleNotFoundError: No module named 'omnigent.opencode_zen_credentials'`

- [ ] **Step 3: Write the implementation**

Create `omnigent/opencode_zen_credentials.py`:

```python
"""Resolution of the OpenCode Zen API key (env → Omnigent keychain).

OpenCode Zen (SST's hosted model gateway, https://opencode.ai/zen) is
authenticated by an API key. OpenCode itself resolves the key from the
``OPENCODE_API_KEY`` env var (or its own ``auth.json`` via ``/connect``);
Omnigent can additionally hold the key in its keychain-backed secret store
(``omnigent setup`` → OpenCode → "Set OpenCode Zen API key") so sessions
work without any ambient env. This module is the single resolver used by
the runner (spawn-env injection), setup reporting, and the sbx passthrough.

Resolution order — ambient env always wins over the keychain:

1. ``OPENCODE_API_KEY``
2. ``OMNIGENT_OPENCODE_API_KEY`` (the standard Omnigent-prefixed alias)
3. Keychain secret ``opencode-zen`` (:mod:`omnigent.onboarding.secrets`)
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnigent.env_credentials import env_names_with_omnigent_prefix

#: Env var OpenCode reads to authenticate its own Zen provider.
OPENCODE_API_KEY_ENV_VAR = "OPENCODE_API_KEY"

#: Keychain secret name ``omnigent setup`` stores the Zen key under.
OPENCODE_ZEN_SECRET_NAME = "opencode-zen"

#: Source tag for a keychain-resolved key.
KEYCHAIN_SOURCE = "keychain"


def resolve_opencode_zen_key(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Resolve the OpenCode Zen API key.

    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``(source, key)`` — source is ``"env:<NAME>"`` or
        :data:`KEYCHAIN_SOURCE` — or ``None`` when no key is configured.
        Keychain read failures count as "no key"; this never raises.
    """
    env = os.environ if environ is None else environ
    for name in env_names_with_omnigent_prefix(OPENCODE_API_KEY_ENV_VAR):
        value = env.get(name, "")
        if value.strip():
            return f"env:{name}", value.strip()
    try:
        # Lazy import mirrors resolve_secret: keeps session launch cheap and
        # avoids pulling keyring in unless the env carries no key.
        from omnigent.onboarding.secrets import load_secret

        stored = load_secret(OPENCODE_ZEN_SECRET_NAME)
    except Exception:
        # A locked/broken keyring must degrade to "no key", never crash launch.
        return None
    if stored and stored.strip():
        return KEYCHAIN_SOURCE, stored.strip()
    return None


def zen_spawn_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Env to merge into a spawned ``opencode`` process.

    Ambient env still wins in the spawned process: an env-resolved key just
    re-stamps the value the child would inherit anyway.

    :param environ: Optional environment mapping; defaults to ``os.environ``.
    :returns: ``{"OPENCODE_API_KEY": <key>}`` when a key resolves, else ``{}``.
    """
    resolved = resolve_opencode_zen_key(environ)
    if resolved is None:
        return {}
    return {OPENCODE_API_KEY_ENV_VAR: resolved[1]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_opencode_zen_credentials.py -v`
Expected: 10 PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/opencode_zen_credentials.py tests/test_opencode_zen_credentials.py
git commit -m "feat(opencode): OpenCode Zen API key resolver (env + keychain)"
```

---

### Task 2: Setup reporter — Zen key in `OpenCodeAuthSummary` and `reachable_provider_ids`

**Files:**
- Modify: `omnigent/onboarding/opencode_auth.py`
- Test: `tests/onboarding/test_opencode_auth.py`

**Interfaces:**
- Consumes (from Task 1): `resolve_opencode_zen_key(environ=None) -> tuple[str, str] | None`, `KEYCHAIN_SOURCE = "keychain"`.
- Produces (Task 4 relies on these):
  - `OpenCodeAuthSummary` gains field `zen_key_source: str | None` (`"env:OPENCODE_API_KEY"` / `"env:OMNIGENT_OPENCODE_API_KEY"` / `"keychain"` / `None`).
  - `OpenCodeAuthSummary.has_provider` is `True` when `zen_key_source` is set (even with no stored/env providers).
  - `describe()` appends a `zen key: <source>` segment.
  - `reachable_provider_ids()` includes `"opencode"` when the Zen key resolves.

- [ ] **Step 1: Write the failing tests**

In `tests/onboarding/test_opencode_auth.py`, first extend the existing autouse fixture `_isolate_env` (lines 13–18) so Zen state can't leak in from the host environment or real keychain. Replace it with:

```python
@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point XDG_DATA_HOME at a tmp dir and clear provider env keys."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for _provider_id, _label, var in oc._ENV_PROVIDER_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", lambda _name: None)
```

Then append these tests at the end of the file:

```python
def test_summary_zen_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen")
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source == "env:OPENCODE_API_KEY"
    assert summary.has_provider
    assert summary.ready


def test_summary_zen_key_from_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source == "keychain"
    assert summary.has_provider


def test_summary_no_zen_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    summary = oc.opencode_auth_summary()
    assert summary.zen_key_source is None
    assert not summary.has_provider


def test_describe_includes_zen_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    text = oc.opencode_auth_summary().describe()
    assert "env: OpenAI" in text
    assert "zen key: keychain" in text


def test_reachable_ids_include_opencode_for_zen_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.load_secret",
        lambda name: "sk-vault" if name == "opencode-zen" else None,
    )
    assert "opencode" in oc.reachable_provider_ids()


def test_reachable_ids_include_opencode_for_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-zen")
    assert "opencode" in oc.reachable_provider_ids()


def test_reachable_ids_omit_opencode_without_key() -> None:
    assert "opencode" not in oc.reachable_provider_ids()
```

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `pytest tests/onboarding/test_opencode_auth.py -v`
Expected: existing tests PASS; the new tests FAIL (`zen_key_source` attribute missing / `TypeError: opencode_auth_summary` or `AttributeError`).

- [ ] **Step 3: Implement in `omnigent/onboarding/opencode_auth.py`**

Add the import (after the existing `harness_install` import at line 24):

```python
from omnigent.opencode_zen_credentials import resolve_opencode_zen_key
```

Update the module docstring's first paragraph — replace the sentence starting "Like :mod:`omnigent.onboarding.goose_auth`, Omnigent stores **no** OpenCode credentials:" so the paragraph reads:

```python
"""OpenCode readiness + credential reporting for ``omnigent setup``.

OpenCode owns its own provider auth via ``opencode auth login`` (stored in
``~/.local/share/opencode/auth.json``) or ambient provider env vars
(``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` / …). Omnigent stores exactly one
optional credential of its own: the OpenCode Zen API key, kept in Omnigent's
keychain and injected as ``OPENCODE_API_KEY`` at spawn (see
:mod:`omnigent.opencode_zen_credentials`) — it never touches ``auth.json``.
This module is otherwise a thin, read-only reporter so ``omnigent setup``
can show which providers OpenCode can reach and offer to run its native
login — without ever touching its secrets.
```

(Keep the docstring's second paragraph about reading `auth.json` directly unchanged.)

In `reachable_provider_ids`, add the Zen check before the `return`:

```python
def reachable_provider_ids(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Return OpenCode provider ids reachable from stored auth + env keys.

    Ids match OpenCode's own (the ``provider/model`` prefix), so callers can
    filter a model list down to what the user can actually authenticate.
    """
    env = os.environ if environ is None else environ
    ids = set(_stored_providers())
    for provider_id, _label, var in _ENV_PROVIDER_VARS:
        if env.get(var, "").strip():
            ids.add(provider_id)
    # The Zen key (env or Omnigent keychain) authenticates OpenCode's own
    # ``opencode`` provider, so its models are reachable too.
    if resolve_opencode_zen_key(environ) is not None:
        ids.add("opencode")
    return frozenset(ids)
```

In `OpenCodeAuthSummary`, add the field (after `env_providers`), update `has_provider`, and extend `describe()`:

```python
    installed: bool
    stored_providers: tuple[str, ...]
    env_providers: tuple[str, ...]
    zen_key_source: str | None = None

    @property
    def has_provider(self) -> bool:
        """Whether any provider is reachable (stored, env key, or Zen key)."""
        return bool(self.stored_providers or self.env_providers or self.zen_key_source)
```

Also extend the class docstring's param list with:

```python
    :param zen_key_source: Where the OpenCode Zen API key resolves from
        (``"env:<NAME>"`` or ``"keychain"``), or ``None`` when absent.
```

In `describe()`, add a third segment before the join:

```python
        if self.zen_key_source:
            parts.append(f"zen key: {self.zen_key_source}")
```

In `opencode_auth_summary()`:

```python
def opencode_auth_summary() -> OpenCodeAuthSummary:
    """Summarize the local OpenCode credential state for setup display."""
    zen = resolve_opencode_zen_key()
    return OpenCodeAuthSummary(
        installed=harness_cli_installed(OPENCODE_KEY),
        stored_providers=_stored_providers(),
        env_providers=_env_providers(),
        zen_key_source=zen[0] if zen is not None else None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/test_opencode_auth.py tests/test_opencode_zen_credentials.py -v`
Expected: all PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/opencode_auth.py tests/onboarding/test_opencode_auth.py
git commit -m "feat(opencode): report the Zen key in setup readiness and reachable providers"
```

---

### Task 3: Runner spawn-env injection

**Files:**
- Modify: `omnigent/runner/app.py` (the opencode-native launch function; anchor: the `seed_opencode_auth(bridge_dir)` call ~line 1180)

**Interfaces:**
- Consumes (from Task 1): `zen_spawn_env() -> dict[str, str]`.
- Produces: the spawned `opencode serve` env carries `OPENCODE_API_KEY` whenever the resolver finds a key. (`OpenCodeNativeServer(extra_env=…)` → `filtered_server_env` overlays `extra_env` on the inherited env; ambient env is inherited anyway, so keychain is the only case that adds anything new.)

- [ ] **Step 1: Add the injection**

In `omnigent/runner/app.py`, directly after the `seed_opencode_auth(bridge_dir)` call (the comment block "The server runs with a per-session XDG_DATA_HOME…"), add:

```python
    # An Omnigent-stored OpenCode Zen key rides the spawn env so the server
    # can authenticate the ``opencode`` provider. Ambient OPENCODE_API_KEY is
    # inherited via filtered_server_env regardless; the resolver returns the
    # ambient value first, so this only adds a key the env doesn't carry.
    from omnigent.opencode_zen_credentials import zen_spawn_env

    policy_env.update(zen_spawn_env())
```

Note: `policy_env` is the dict initialized as `policy_env: dict[str, str] = {}` earlier in the same function and passed to `OpenCodeNativeServer(extra_env=policy_env or None)`. Keep the import local, matching the function's existing lazy-import style.

- [ ] **Step 2: Run the existing runner + bridge tests to verify no regression**

Run: `pytest tests/runner/test_app_sessions_native.py tests/test_opencode_native_app_server.py -q`
Expected: all PASS (behavior is additive; `zen_spawn_env()` returns `{}` in test environments with no key).

- [ ] **Step 3: Verify the injection point manually**

Run: `grep -n "zen_spawn_env" omnigent/runner/app.py`
Expected: two hits (import + `policy_env.update`), located after the `seed_opencode_auth(bridge_dir)` line and before `server = OpenCodeNativeServer(`.

- [ ] **Step 4: Commit**

```bash
git add omnigent/runner/app.py
git commit -m "feat(opencode): inject the stored Zen key into the opencode serve spawn env"
```

---

### Task 4: Setup wizard — paste/clear Zen key actions

**Files:**
- Modify: `omnigent/cli.py` (`_manage_opencode_harness` ~line 10865, `_print_opencode_auth_help` ~line 10848; new helpers next to them)
- Test: `tests/cli/test_opencode_setup.py`

**Interfaces:**
- Consumes (Task 1): `OPENCODE_ZEN_SECRET_NAME`, `KEYCHAIN_SOURCE`; (Task 2): `OpenCodeAuthSummary.zen_key_source`.
- Consumes (existing): `omnigent.onboarding.secrets.store_secret / delete_secret / active_backend`, `omnigent.onboarding.interactive.prompt_text(label, *, hide_input=False) -> str`, `_HarnessMenuRow(label, action=...)`.
- Produces: two module-level cli helpers `_prompt_opencode_zen_key() -> str | None` and `_clear_opencode_zen_key() -> str`, plus menu wiring.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cli/test_opencode_setup.py`:

```python
def test_prompt_zen_key_stores_stripped_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: dict[str, str] = {}
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.store_secret",
        lambda name, value: stored.update({name: value}),
    )
    monkeypatch.setattr("omnigent.onboarding.secrets.active_backend", lambda: "keyring")
    monkeypatch.setattr(
        "omnigent.onboarding.interactive.prompt_text", lambda *a, **k: " sk-zen-123 \n"
    )
    status = cli._prompt_opencode_zen_key()
    assert stored == {"opencode-zen": "sk-zen-123"}
    assert status == "✓ Zen key stored (keyring)"


def test_prompt_zen_key_empty_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.onboarding.secrets.store_secret",
        lambda name, value: calls.append(name),
    )
    monkeypatch.setattr("omnigent.onboarding.interactive.prompt_text", lambda *a, **k: "  ")
    assert cli._prompt_opencode_zen_key() is None
    assert calls == []


def test_clear_zen_key_deletes_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr("omnigent.onboarding.secrets.delete_secret", deleted.append)
    assert cli._clear_opencode_zen_key() == "✓ Zen key cleared"
    assert deleted == ["opencode-zen"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/cli/test_opencode_setup.py -v`
Expected: existing tests PASS; the three new tests FAIL with `AttributeError: module 'omnigent.cli' has no attribute '_prompt_opencode_zen_key'` (and `_clear_opencode_zen_key`).

- [ ] **Step 3: Implement the helpers in `omnigent/cli.py`**

Add directly above `_print_opencode_auth_help` (~line 10848), matching the cursor drill-in's paste pattern (`prompt_text` + `store_secret`):

```python
def _prompt_opencode_zen_key() -> str | None:
    """Paste-and-store the OpenCode Zen API key; return a status line.

    Stored in Omnigent's keychain (``opencode-zen``) and injected as
    ``OPENCODE_API_KEY`` when the opencode server spawns — never written to
    opencode's own ``auth.json``. An ambient ``OPENCODE_API_KEY`` always
    wins over the stored key at resolution time.
    """
    from omnigent.onboarding import secrets as secret_store
    from omnigent.onboarding.interactive import prompt_text
    from omnigent.opencode_zen_credentials import OPENCODE_ZEN_SECRET_NAME

    pasted = prompt_text("OpenCode Zen API key (OPENCODE_API_KEY)", hide_input=True).strip()
    if not pasted:
        return None
    secret_store.store_secret(OPENCODE_ZEN_SECRET_NAME, pasted)
    return f"✓ Zen key stored ({secret_store.active_backend()})"


def _clear_opencode_zen_key() -> str:
    """Delete the stored OpenCode Zen key from Omnigent's keychain."""
    from omnigent.onboarding import secrets as secret_store
    from omnigent.opencode_zen_credentials import OPENCODE_ZEN_SECRET_NAME

    secret_store.delete_secret(OPENCODE_ZEN_SECRET_NAME)
    return "✓ Zen key cleared"
```

- [ ] **Step 4: Wire the menu rows in `_manage_opencode_harness`**

In the `rows` list (currently login / model / list / help / back), insert the Zen rows after the login row. The clear row appears only when the stored key is what resolves (`zen_key_source == "keychain"` — an env key can't be cleared from here). Replace the `rows` construction with:

```python
        rows: list[_HarnessMenuRow] = [
            _HarnessMenuRow("Run opencode auth login", action="login"),
            _HarnessMenuRow("Set OpenCode Zen API key", action="zen"),
        ]
        if summary.zen_key_source == "keychain":
            rows.append(_HarnessMenuRow("Clear stored Zen key", action="zen-clear"))
        rows.extend(
            [
                _HarnessMenuRow(model_label, action="model"),
                _HarnessMenuRow("List providers & credentials", action="list"),
                _HarnessMenuRow("Show provider options", action="help"),
                _HarnessMenuRow("← Back", action="back"),
            ]
        )
```

And extend the action dispatch (after the `action == "login"` branch):

```python
        elif action == "zen":
            status = _prompt_opencode_zen_key()
        elif action == "zen-clear":
            status = _clear_opencode_zen_key()
```

Also update `_manage_opencode_harness`'s docstring sentence "it never stores a key through Omnigent" to:

```
    reach and offers to launch its native login. The one key Omnigent stores
    itself is the optional OpenCode Zen API key (kept in Omnigent's keychain,
    injected as ``OPENCODE_API_KEY`` at spawn — never written to opencode's
    ``auth.json``).
```

- [ ] **Step 5: Update the help text**

In `_print_opencode_auth_help`, replace the line
`"  Omnigent stores no OpenCode credential of its own.\n"` with:

```python
        "    • [bold]OpenCode Zen[/bold] — paste an API key ('Set OpenCode Zen API key');\n"
        "      kept in Omnigent's keychain, injected as OPENCODE_API_KEY at launch.\n"
        "  Omnigent stores only the optional Zen key — everything else stays with opencode.\n"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/cli/test_opencode_setup.py -v`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add omnigent/cli.py tests/cli/test_opencode_setup.py
git commit -m "feat(opencode): set/clear the OpenCode Zen key from the setup drill-in"
```

---

### Task 5: sbx sandbox passthrough fallback

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py` (`_resolve_env` ~line 415; `SANDBOX_ENV_PASSTHROUGH_ENV_VAR` docstring ~line 57)
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes (Task 1): `OPENCODE_API_KEY_ENV_VAR`, `resolve_opencode_zen_key`.
- Produces: naming `OPENCODE_API_KEY` in `OMNIGENT_SBX_SANDBOX_ENV` (or the `env=` constructor arg) no longer fails loud when the key lives only in the keychain.

- [ ] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py` (it already imports `sbxmod` and `SbxSandboxLauncher`):

```python
def test_resolve_env_opencode_key_falls_back_to_keychain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(
        sbxmod, "resolve_opencode_zen_key", lambda environ=None: ("keychain", "sk-vault")
    )
    resolved = SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()
    assert resolved == {"OPENCODE_API_KEY": "sk-vault"}


def test_resolve_env_opencode_key_prefers_local_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-local")
    monkeypatch.setattr(
        sbxmod, "resolve_opencode_zen_key", lambda environ=None: ("keychain", "sk-vault")
    )
    resolved = SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()
    assert resolved == {"OPENCODE_API_KEY": "sk-local"}


def test_resolve_env_opencode_key_missing_everywhere_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.setattr(sbxmod, "resolve_opencode_zen_key", lambda environ=None: None)
    with pytest.raises(click.ClickException):
        SbxSandboxLauncher(env=["OPENCODE_API_KEY"])._resolve_env()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -v`
Expected: the first new test FAILS (`AttributeError: module … has no attribute 'resolve_opencode_zen_key'`); existing tests PASS.

- [ ] **Step 3: Implement the fallback**

In `omnigent/onboarding/sandboxes/sbx.py`, add to the imports (next to the existing `harness_install` import):

```python
from omnigent.opencode_zen_credentials import (
    OPENCODE_API_KEY_ENV_VAR,
    resolve_opencode_zen_key,
)
```

Replace the body of `_resolve_env`'s loop:

```python
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None and name == OPENCODE_API_KEY_ENV_VAR:
                # The Zen key may live in Omnigent's keychain rather than the
                # local environment — resolve it before failing loud.
                zen = resolve_opencode_zen_key()
                if zen is not None:
                    value = zen[1]
            if value is None:
                raise click.ClickException(
                    f"sbx env passthrough names '{name}' but it is not set in the "
                    f"local environment — set it (or remove it from "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved
```

Extend the `SANDBOX_ENV_PASSTHROUGH_ENV_VAR` docstring (~line 57) with one sentence at the end:

```python
"""Environment variable naming (comma-separated) the LOCAL environment
variables whose values are forwarded into the in-sandbox host process
(``sbx exec -e NAME=VALUE``) — typically harness LLM credentials
(``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, gateway URLs).
Names, not values: read from the invoking shell at exec time.
``OPENCODE_API_KEY`` additionally falls back to the Omnigent-keychain
OpenCode Zen key when not set locally."""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): resolve OPENCODE_API_KEY from the Omnigent keychain for sandbox passthrough"
```

---

### Task 6: Full-suite check + live Zen verification

**Files:**
- None created; verification only.

**Interfaces:**
- Consumes: everything from Tasks 1–5.

- [ ] **Step 1: Run the full affected test set**

Run: `pytest tests/test_opencode_zen_credentials.py tests/onboarding/test_opencode_auth.py tests/cli/test_opencode_setup.py tests/onboarding/sandboxes/test_sbx.py tests/runner/test_app_sessions_native.py tests/test_opencode_native_app_server.py tests/test_opencode_native_bridge.py -q`
Expected: all PASS.

- [ ] **Step 2: Run pre-commit across the changed files**

Run: `pre-commit run --all-files`
Expected: all hooks pass (fix and re-commit if not).

- [ ] **Step 3: Live verification (needs a real Zen key — the Approach-A risk check)**

This step requires the user's actual OpenCode Zen API key; if none is available, report it as pending user verification instead of skipping silently.

With the key exported (never echo or log the value):

1. Run: `env OPENCODE_API_KEY="$ZEN_KEY" opencode models` → expect `opencode/…` model ids in the output.
2. `omnigent setup` → OpenCode drill-in → "Set OpenCode Zen API key" → paste; the header should then show `zen key: keychain`.
3. With NO ambient `OPENCODE_API_KEY`, start an opencode session (`omni opencode`) pinned to an `opencode/…` model and run one real turn — it must complete without opencode prompting for auth.

If step 1 or 3 shows opencode ignoring the env var: STOP and report — the spec's fallback (merging the Zen entry into the per-session seeded `auth.json` copy inside `seed_opencode_auth`) replaces Task 3's delivery, and needs a plan amendment before implementing.

- [ ] **Step 4: Final commit (only if verification produced fixes)**

```bash
git status
```

Commit any fixes with a `fix(opencode): …` message; nothing to commit when verification passed clean.
