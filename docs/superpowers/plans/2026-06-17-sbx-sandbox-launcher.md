# Docker `sbx` Sandbox Launcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `sbx` sandbox launcher that runs an Omnigent host inside a local Docker Sandbox microVM, so an Omnigent session can safely run commands (and nested Docker dev containers) in the sandbox and be controlled exactly like any claude-native session.

**Architecture:** A new `SbxSandboxLauncher` implements the provider-agnostic `SandboxLauncher` contract (`omnigent/onboarding/sandboxes/base.py`) by shelling out to the local `sbx` CLI via `subprocess` — there is no Python SDK. The existing `bootstrap.py` flow (`prepare → provision → keep_alive → build_wheels → ship_wheels → login`) and `omnigent sandbox create/connect` CLI drive it unchanged, except for a new repeatable `--kit` option on `create`.

**Tech Stack:** Python 3.12+, `click`, `subprocess`/`shutil`, the external `sbx` binary (v0.32.0+), pytest.

## Global Constraints

- Python 3.12+ (`from __future__ import annotations` at top of every module, matching sibling launchers).
- No new third-party/optional dependency — `sbx` is an external binary located with `shutil.which`; no `pyproject.toml` extra.
- All launcher methods raise `click.ClickException` (with a remediation hint) on failure — never a raw exception — matching `daytona.py`/`modal.py`.
- Provider name is exactly `"sbx"`.
- `supports_local_port_forward = False`, `supports_cli_bootstrap = True`, `wheel_build_index_url = None`.
- Sandboxes are name-addressed: `provision` returns the sandbox **name** as its id.
- Tests use hand-rolled recording fakes (never `MagicMock`) and assert exact argv, mirroring `tests/onboarding/sandboxes/test_daytona.py`.
- Follow TDD: write the failing test, watch it fail, implement minimally, watch it pass, commit.

---

### Task 1: Discovery spike — resolve the runtime unknowns against a real `sbx`

This is a discovery task (no production code). Its deliverable is **recorded findings** that confirm or adjust four constants/branches used by later tasks. Requires a machine with `sbx` installed, Docker running, and `sbx login` completed.

**Files:**
- Modify: `docs/superpowers/specs/2026-06-17-sbx-sandbox-launcher-design.md` (fill in the "Open verification items" section with results)

- [x] **Step 1: Confirm not-logged-in detection (already verified once)**

Run: `sbx ls; echo "exit=$?"`
Expected when logged out: `exit=1` and stderr contains `Not authenticated to Docker` / `Sign in with: sbx login`.
Record: the exact exit code + message `prepare()` keys on. (Pre-verified: exit 1, message as above.)

- [x] **Step 2: Inspect the default `shell` sandbox image contents**

```bash
sbx create --name oa-probe shell .
sbx exec oa-probe bash -lc 'for b in python3 pip3 git tmux node npm docker claude; do printf "%s: " "$b"; command -v "$b" || echo MISSING; done'
sbx exec oa-probe bash -lc 'cat /etc/os-release | head -2; python3 --version 2>&1; pip3 --version 2>&1'
sbx exec -u root oa-probe bash -lc 'command -v apt-get || echo NO-APT'
# PEP 668 check — does a user pip install need --break-system-packages?
sbx exec oa-probe bash -lc 'test -f "$(python3 -c "import sys,sysconfig,os;print(os.path.join(sysconfig.get_path(\"stdlib\"),\"EXTERNALLY-MANAGED\"))")" && echo PEP668-MANAGED || echo PEP668-OK'
```

Record in the spec: which of `python3/pip3/git/tmux/node/npm/docker/claude` are present, the base distro (apt vs not), and whether PEP 668 is in force.

- [x] **Step 3: Confirm whether local sandboxes idle-stop**

Run: `sbx ls --json` and inspect for any idle/auto-stop fields; check `sbx create --help` / docs.
Record: whether `keep_alive` needs to do anything (default assumption: no idle-stop ⇒ informational no-op).

- [x] **Step 4: Confirm the kit reference forms accepted at create**

```bash
git clone https://github.com/landreville/sbxkit /tmp/sbxkit
sbx kit validate /tmp/sbxkit/claude; echo "exit=$?"
sbx create --name oa-probe-kit --kit /tmp/sbxkit/claude shell .; echo "exit=$?"
```

Record: that local-directory kit refs work (pre-verified: the GitHub `/tree/...` URL is rejected as an OCI ref by `sbx kit validate`). Confirm passthrough of a local dir / OCI ref; document the recommended way to consume a repo-subdir kit (clone locally, then pass the directory).

- [x] **Step 5: Clean up probes**

Run: `sbx rm -f oa-probe oa-probe-kit`

- [x] **Step 6: Update the spec's "Open verification items" with the four results, then commit**

```bash
git add docs/superpowers/specs/2026-06-17-sbx-sandbox-launcher-design.md
git commit -m "docs: record sbx launcher discovery findings"
```

> **Adjustment rule for later tasks:** if Step 2 shows PEP 668 is *not* in force, drop `--break-system-packages` from `wheel_install_command` (Task 6). If a tool is already present in the base image, it simply no-ops in the idempotent setup script (Task 4) — no code change needed. If Step 3 shows an idle-stop exists, document it in `keep_alive` (Task 8) like Modal's lifetime note.

---

### Task 2: Module scaffold — class, class vars, constructor, config resolution

**Files:**
- Create: `omnigent/onboarding/sandboxes/sbx.py`
- Create: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Produces:
  - `SbxSandboxLauncher(*, template: str | None = None, env: Sequence[str] | None = None, kits: Sequence[str] | None = None)`
  - Class vars `provider="sbx"`, `supports_cli_bootstrap=True`, `supports_local_port_forward=False`, `wheel_build_index_url=None`
  - Module constants `TEMPLATE_ENV_VAR="OMNIGENT_SBX_TEMPLATE"`, `SANDBOX_ENV_PASSTHROUGH_ENV_VAR="OMNIGENT_SBX_SANDBOX_ENV"`, `KITS_ENV_VAR="OMNIGENT_SBX_KITS"`
  - Internal helpers: `_sbx_binary() -> str`, `_run_sbx(args, *, capture=True, check=False) -> subprocess.CompletedProcess`, `_resolve_template() -> str | None`, `_resolve_kits() -> list[str]`, `_resolve_env() -> dict[str, str]`

- [x] **Step 1: Write the failing test (config resolution + class vars)**

Create `tests/onboarding/sandboxes/test_sbx.py`:

```python
"""Tests for :mod:`omnigent.onboarding.sandboxes.sbx`."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest

import omnigent.onboarding.sandboxes.sbx as sbxmod
from omnigent.onboarding.sandboxes.sbx import (
    KITS_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    TEMPLATE_ENV_VAR,
    SbxSandboxLauncher,
)


# ── Fake sbx CLI ────────────────────────────────────────────
#
# All transport is `subprocess.run([sbx, ...])`. The fake records every
# invocation's argv and hands back canned CompletedProcess results keyed
# by the sbx subcommand (argv index 1: "create"/"exec"/"cp"/"ls"/"rm").
# Hand-rolled (never MagicMock) so attribute access is explicit.


@dataclass
class _SbxCall:
    """One recorded subprocess.run invocation."""

    args: list[str]
    capture: bool


@dataclass
class _FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    args: list[str]
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _FakeSbx:
    """Recorder injected in place of subprocess.run."""

    calls: list[_SbxCall] = field(default_factory=list)
    responses: dict[str, _FakeCompleted] = field(default_factory=dict)
    raise_on: dict[str, BaseException] = field(default_factory=dict)

    def run(
        self,
        argv: list[str],
        *,
        capture_output: bool = False,
        text: bool = False,
        check: bool = False,
        **_: object,
    ) -> _FakeCompleted:
        self.calls.append(_SbxCall(args=list(argv), capture=capture_output))
        sub = argv[1] if len(argv) > 1 else ""
        if sub in self.raise_on:
            raise self.raise_on[sub]
        resp = self.responses.get(sub, _FakeCompleted(args=list(argv)))
        return _FakeCompleted(list(argv), resp.returncode, resp.stdout, resp.stderr)


@pytest.fixture()
def fake_sbx(monkeypatch: pytest.MonkeyPatch) -> _FakeSbx:
    """Install the fake sbx with the binary present and clean ambient config."""
    monkeypatch.setattr(sbxmod.shutil, "which", lambda name: "/usr/bin/sbx")
    fake = _FakeSbx()
    monkeypatch.setattr(sbxmod.subprocess, "run", fake.run)
    for var in (TEMPLATE_ENV_VAR, SANDBOX_ENV_PASSTHROUGH_ENV_VAR, KITS_ENV_VAR):
        monkeypatch.delenv(var, raising=False)
    return fake


# ── class vars / config resolution ──────────────────────────


def test_capability_class_vars() -> None:
    """sbx is a local CLI-bootstrap provider with no port-forward path."""
    assert SbxSandboxLauncher.provider == "sbx"
    assert SbxSandboxLauncher.supports_cli_bootstrap is True
    assert SbxSandboxLauncher.supports_local_port_forward is False


def test_resolve_kits_constructor_wins(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructor kits win over the env-var fallback."""
    monkeypatch.setenv(KITS_ENV_VAR, "env-kit")
    launcher = SbxSandboxLauncher(kits=["a", "b"])
    assert launcher._resolve_kits() == ["a", "b"]


def test_resolve_kits_env_fallback(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without constructor kits, comma/whitespace-separated env names apply."""
    monkeypatch.setenv(KITS_ENV_VAR, "kit-one , kit-two")
    assert SbxSandboxLauncher()._resolve_kits() == ["kit-one", "kit-two"]


def test_resolve_env_missing_var_fails_loud(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured passthrough name unset locally is a loud error."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(click.ClickException, match="OPENAI_API_KEY"):
        SbxSandboxLauncher(env=["OPENAI_API_KEY"])._resolve_env()


def test_resolve_env_resolves_values(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough names resolve to values from the local environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert SbxSandboxLauncher(env=["ANTHROPIC_API_KEY"])._resolve_env() == {
        "ANTHROPIC_API_KEY": "sk-test"
    }
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -v`
Expected: FAIL — `ModuleNotFoundError: omnigent.onboarding.sandboxes.sbx`.

- [x] **Step 3: Write the module scaffold**

Create `omnigent/onboarding/sandboxes/sbx.py`:

```python
"""
Docker Sandbox (``sbx``) launcher.

Implements the CLI-bootstrap subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for local
`Docker Sandboxes <https://docs.docker.com/ai/sandboxes/>`_ — microVMs
driven by the external ``sbx`` binary. Unlike the cloud launchers
(Daytona, Modal) there is no Python SDK: all transport is ``subprocess``
against ``sbx``.

Platform notes that shape this launcher:

- **Local, not cloud.** Sandboxes run on the machine invoking the CLI,
  so this is a CLI-bootstrap provider only (``omnigent sandbox
  create/connect``). There is no server-managed flow.
- **Own Docker daemon per sandbox.** Each sbx sandbox is a microVM with
  its own Docker daemon, so the in-session agent can run nested Docker
  dev containers safely.
- **Default image + full install.** Sandboxes boot sbx's default image
  (no prebaked omnigent), so :meth:`wheel_install_command` does a full
  dependency install and :meth:`provision` runs a one-time setup step to
  install the host's runtime deps + the Claude Code CLI.
- **No inbound port forwarding.** ``supports_local_port_forward`` stays
  ``False`` (matching Modal), so the CLI auto-skips the Databricks App
  OAuth step.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from collections.abc import Sequence
from typing import ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
)

# ── Constants ──────────────────────────────────────────

TEMPLATE_ENV_VAR: str = "OMNIGENT_SBX_TEMPLATE"
"""Environment variable naming a container image to boot sbx sandboxes
from (``sbx create -t``), overriding sbx's default agent image."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_SBX_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the LOCAL environment
variables whose values are forwarded into the in-sandbox host process
(``sbx exec -e NAME=VALUE``) — typically harness LLM credentials
(``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, gateway URLs).
Names, not values: read from the invoking shell at exec time."""

KITS_ENV_VAR: str = "OMNIGENT_SBX_KITS"
"""Environment variable naming (comma- or whitespace-separated) sbx kit
references applied at provision time (``sbx create --kit``)."""

# One-time root setup run after `sbx create` to make sbx's default image
# a viable Omnigent host: ensure python/pip, git, tmux, node/npm, and the
# Claude Code CLI. Idempotent — already-present tools no-op. (Discovery
# task confirms the base image's gaps; absent tools are installed, present
# ones skipped.)
_SETUP_COMMAND: str = (
    "set -e; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "export DEBIAN_FRONTEND=noninteractive; pkgs=''; "
    'command -v python3 >/dev/null 2>&1 || pkgs="$pkgs python3"; '
    'command -v pip3 >/dev/null 2>&1 || pkgs="$pkgs python3-pip"; '
    'command -v git >/dev/null 2>&1 || pkgs="$pkgs git"; '
    'command -v tmux >/dev/null 2>&1 || pkgs="$pkgs tmux"; '
    'command -v node >/dev/null 2>&1 || pkgs="$pkgs nodejs"; '
    'command -v npm >/dev/null 2>&1 || pkgs="$pkgs npm"; '
    'if [ -n "$pkgs" ]; then apt-get update && '
    "apt-get install -y --no-install-recommends $pkgs; fi; fi; "
    "command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code"
)


class SbxSandboxLauncher(SandboxLauncher):
    """:class:`SandboxLauncher` for local Docker Sandboxes via ``sbx``."""

    provider: ClassVar[str] = "sbx"
    supports_cli_bootstrap: ClassVar[bool] = True
    # sbx microVMs expose no local->sandbox port-forward path, so the
    # Databricks App OAuth flow is unsupported (CLI auto-skips it).
    supports_local_port_forward: ClassVar[bool] = False
    wheel_build_index_url: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        template: str | None = None,
        env: Sequence[str] | None = None,
        kits: Sequence[str] | None = None,
    ) -> None:
        """
        :param template: Container image for ``sbx create -t``, or
            ``None`` to resolve :data:`TEMPLATE_ENV_VAR` / sbx default.
        :param env: Local env-var NAMES forwarded into the host process,
            or ``None`` to resolve :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param kits: sbx kit references applied at create, or ``None`` to
            resolve :data:`KITS_ENV_VAR`.
        """
        self._template = template
        self._env_names = tuple(env) if env is not None else None
        self._kits = tuple(kits) if kits is not None else None
        self._binary: str | None = None

    def _sbx_binary(self) -> str:
        """Locate the ``sbx`` binary, caching the result."""
        if self._binary is None:
            found = shutil.which("sbx")
            if found is None:
                raise click.ClickException(
                    "The `sbx` CLI is required for the 'sbx' sandbox provider. "
                    "Install Docker Sandboxes (https://docs.docker.com/ai/sandboxes/) "
                    "and sign in with `sbx login`."
                )
            self._binary = found
        return self._binary

    def _run_sbx(
        self, args: list[str], *, capture: bool = True, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run ``sbx <args>``; capture text output by default."""
        return subprocess.run(
            [self._sbx_binary(), *args],
            capture_output=capture,
            text=True,
            check=check,
        )

    def _resolve_template(self) -> str | None:
        """Resolve the create image: constructor wins, then env var."""
        return self._template or os.environ.get(TEMPLATE_ENV_VAR) or None

    def _resolve_kits(self) -> list[str]:
        """Resolve kit references: constructor wins, then env var."""
        if self._kits is not None:
            return list(self._kits)
        raw = os.environ.get(KITS_ENV_VAR, "")
        return [k.strip() for k in raw.replace(",", " ").split() if k.strip()]

    def _resolve_env(self) -> dict[str, str]:
        """Resolve passthrough NAMES to local values; missing fails loud."""
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                n.strip()
                for n in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if n.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sbx env passthrough names '{name}' but it is not set in the "
                    f"local environment — set it (or remove it from {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -v`
Expected: PASS (5 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): scaffold launcher with config resolution"
```

---

### Task 3: `prepare()` — binary + login preflight

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `_sbx_binary`, `_run_sbx` (Task 2)
- Produces: `prepare(self) -> None`

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── prepare ─────────────────────────────────────────────────


def test_prepare_requires_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `sbx` on PATH → loud error with an install hint."""
    monkeypatch.setattr(sbxmod.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException, match="sbx"):
        SbxSandboxLauncher().prepare()


def test_prepare_requires_login(fake_sbx: _FakeSbx) -> None:
    """`sbx ls` failing (not authenticated) → error pointing to `sbx login`."""
    fake_sbx.responses["ls"] = _FakeCompleted(
        args=[], returncode=1, stderr="ERROR: Not authenticated to Docker"
    )
    with pytest.raises(click.ClickException, match="sbx login"):
        SbxSandboxLauncher().prepare()


def test_prepare_passes_when_logged_in(fake_sbx: _FakeSbx) -> None:
    """Binary present + `sbx ls` succeeds → preflight passes; ls was probed."""
    SbxSandboxLauncher().prepare()
    assert [c.args[1] for c in fake_sbx.calls] == ["ls"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k prepare -v`
Expected: FAIL — `prepare` not implemented (raises base capability error / AttributeError).

- [x] **Step 3: Implement `prepare`**

Add to `SbxSandboxLauncher`:

```python
    def prepare(self) -> None:
        """
        Local preflight: the ``sbx`` binary must be installed and signed
        in (``sbx login``).

        :raises click.ClickException: When ``sbx`` is missing or the
            login probe (``sbx ls``) fails.
        """
        self._sbx_binary()  # raises the install hint when absent
        probe = self._run_sbx(["ls"])
        if probe.returncode != 0:
            raise click.ClickException(
                "Not signed in to Docker Sandboxes. Run `sbx login` first."
                + (f" ({probe.stderr.strip()})" if probe.stderr.strip() else "")
            )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k prepare -v`
Expected: PASS (3 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): add prepare preflight (binary + login)"
```

---

### Task 4: `provision()` — create with kits/template + one-time setup

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `_run_sbx`, `_resolve_kits`, `_resolve_template`, `_SETUP_COMMAND` (Tasks 2)
- Produces: `provision(self, name: str) -> str` (returns the sandbox name; bind-mounts cwd)

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── provision ───────────────────────────────────────────────


def test_provision_create_argv_and_setup(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Provision creates a `shell` sandbox bind-mounting cwd, returns the
    name as the id, then runs the one-time root setup step.
    """
    monkeypatch.chdir(tmp_path)
    sandbox_id = SbxSandboxLauncher().provision("omnigent-host")

    assert sandbox_id == "omnigent-host"
    create, setup = fake_sbx.calls
    assert create.args[1:] == [
        "create",
        "--name",
        "omnigent-host",
        "shell",
        str(tmp_path),
    ]
    # Setup runs as root via exec.
    assert setup.args[1:4] == ["exec", "-u", "root"]
    assert setup.args[4] == "omnigent-host"
    assert "@anthropic-ai/claude-code" in setup.args[-1]


def test_provision_includes_kits_and_template(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each kit gets a `--kit`, and the template image rides `-t`, before `shell`."""
    monkeypatch.chdir(tmp_path)
    SbxSandboxLauncher(
        kits=["/tmp/sbxkit/claude", "ghcr.io/me/kit:1"], template="me/img:2"
    ).provision("box")

    create = fake_sbx.calls[0]
    assert create.args[1:] == [
        "create",
        "--name",
        "box",
        "--kit",
        "/tmp/sbxkit/claude",
        "--kit",
        "ghcr.io/me/kit:1",
        "-t",
        "me/img:2",
        "shell",
        str(tmp_path),
    ]


def test_provision_wraps_create_failure(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing `sbx create` surfaces stderr as a ClickException; no setup runs."""
    monkeypatch.chdir(tmp_path)
    fake_sbx.responses["create"] = _FakeCompleted(
        args=[], returncode=1, stderr="Docker is not running"
    )
    with pytest.raises(click.ClickException, match="Docker is not running"):
        SbxSandboxLauncher().provision("box")
    assert [c.args[1] for c in fake_sbx.calls] == ["create"]


def test_provision_missing_network_policy_gives_remediation(
    fake_sbx: _FakeSbx, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Discovery finding: sbx refuses to start a sandbox until a default
    network policy is set. provision rewrites that into a `sbx policy
    set-default` remediation instead of echoing the raw error.
    """
    monkeypatch.chdir(tmp_path)
    fake_sbx.responses["create"] = _FakeCompleted(
        args=[],
        returncode=1,
        stderr="ERROR: default network policy has not been configured",
    )
    with pytest.raises(click.ClickException, match="sbx policy set-default"):
        SbxSandboxLauncher().provision("box")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k provision -v`
Expected: FAIL — `provision` not implemented.

- [x] **Step 3: Implement `provision`**

Add to `SbxSandboxLauncher`:

```python
    def provision(self, name: str) -> str:
        """
        Create a `shell` sbx sandbox bind-mounting the current directory,
        then run the one-time setup step that installs the host runtime
        deps + the Claude Code CLI.

        :param name: Sandbox name (also its id — sbx is name-addressed).
        :returns: The sandbox name.
        :raises click.ClickException: When ``sbx create`` fails.
        """
        workspace = str(Path.cwd())
        args = ["create", "--name", name]
        for kit in self._resolve_kits():
            args += ["--kit", kit]
        template = self._resolve_template()
        if template:
            args += ["-t", template]
        args += ["shell", workspace]

        click.echo(f"▸ Creating sbx sandbox '{name}' (workspace: {workspace})")
        result = self._run_sbx(args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            # Discovery finding: sbx refuses to start any sandbox until a
            # host-global default network policy is set, with a distinctive
            # message. Rewrite it into a single actionable remediation so the
            # user isn't left guessing which `sbx policy` invocation to run.
            if "default network policy" in detail.lower():
                raise click.ClickException(
                    "sbx has no default network policy configured. Run "
                    "`sbx policy set-default balanced` (allows AI services + "
                    "package registries; use `allow-all` if your --server host "
                    "gets blocked), then retry."
                )
            raise click.ClickException(f"sbx sandbox creation failed: {detail}")

        click.echo("  → installing host runtime dependencies")
        setup = self._run_sbx(["exec", "-u", "root", name, "bash", "-lc", _SETUP_COMMAND])
        if setup.returncode != 0:
            raise click.ClickException(
                f"sbx sandbox setup failed on '{name}': "
                f"{setup.stderr.strip() or setup.stdout.strip()}"
            )
        click.echo(f"  → created {name}")
        return name
```

Add `from pathlib import Path` to the module imports (top of file, with the other stdlib imports).

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k provision -v`
Expected: PASS (4 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): provision create + kits/template + setup step"
```

---

### Task 5: `run()` and `put()` — command exec + file shipping

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `_run_sbx` (Task 2), `RemoteCommandResult` (base)
- Produces: `run(self, sandbox_id, command, *, check=True) -> RemoteCommandResult`; `put(self, sandbox_id, local_path, remote_path) -> None`

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── run / put ───────────────────────────────────────────────


def test_run_execs_via_bash_lc(fake_sbx: _FakeSbx) -> None:
    """run wraps the command in `sbx exec <id> bash -lc <cmd>` and returns output."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=0, stdout="/root\n")
    result = SbxSandboxLauncher().run("box", 'printf %s "$HOME"')
    assert result.returncode == 0
    assert result.stdout == "/root\n"
    [call] = fake_sbx.calls
    assert call.args[1:] == ["exec", "box", "bash", "-lc", 'printf %s "$HOME"']


def test_run_check_raises_on_nonzero(fake_sbx: _FakeSbx) -> None:
    """check=True raises naming the command; check=False returns the failure."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=2, stderr="boom")
    with pytest.raises(click.ClickException, match="exit 2"):
        SbxSandboxLauncher().run("box", "false")
    result = SbxSandboxLauncher().run("box", "false", check=False)
    assert result.returncode == 2
    assert result.stderr == "boom"


def test_put_uses_sbx_cp(fake_sbx: _FakeSbx) -> None:
    """put copies a local file to `<id>:<remote>` via `sbx cp`."""
    SbxSandboxLauncher().put("box", Path("/tmp/oa-wheels.tgz"), "/tmp/oa-wheels.tgz")
    [call] = fake_sbx.calls
    assert call.args[1:] == ["cp", "/tmp/oa-wheels.tgz", "box:/tmp/oa-wheels.tgz"]


def test_put_wraps_failure(fake_sbx: _FakeSbx) -> None:
    """A failing `sbx cp` surfaces stderr as a ClickException."""
    fake_sbx.responses["cp"] = _FakeCompleted(args=[], returncode=1, stderr="no such sandbox")
    with pytest.raises(click.ClickException, match="no such sandbox"):
        SbxSandboxLauncher().put("box", Path("/tmp/x"), "/tmp/x")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "run_ or put" -v`
Expected: FAIL — methods not implemented.

- [x] **Step 3: Implement `run` and `put`**

Add to `SbxSandboxLauncher`:

```python
    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox via ``sbx exec`` and capture
        its output (stdout/stderr kept separate).

        :param sandbox_id: Target sandbox name.
        :param command: Shell command (``bash -lc`` applies login PATH).
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: When *check* and the exit is non-zero.
        """
        result = self._run_sbx(["exec", sandbox_id, "bash", "-lc", command])
        for stream in (result.stdout, result.stderr):
            for line in (stream or "").splitlines():
                if line.strip():
                    click.echo(line)
        if check and result.returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {result.returncode}): {command}"
            )
        return RemoteCommandResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via ``sbx cp``.

        :param sandbox_id: Target sandbox name.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path inside the sandbox.
        :raises click.ClickException: When the transfer fails.
        """
        result = self._run_sbx(["cp", str(local_path), f"{sandbox_id}:{remote_path}"])
        if result.returncode != 0:
            raise click.ClickException(
                f"File copy to sandbox '{sandbox_id}' failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "run_ or put" -v`
Expected: PASS (4 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): add run (exec) and put (cp) primitives"
```

---

### Task 6: `wheel_install_command()` — full install on the default image

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Produces: `wheel_install_command(self, remote_tgz_path: str) -> str`

- [x] **Step 1: Write the failing test**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── wheel_install_command ───────────────────────────────────


def test_wheel_install_is_full_install(fake_sbx: _FakeSbx) -> None:
    """
    The default sbx image has no baked omnigent, so the install is a
    FULL dependency install into the user site — NOT the host-image
    overlay (no --no-deps / --force-reinstall).
    """
    cmd = SbxSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in cmd
    assert "pip install" in cmd
    assert "--user" in cmd
    assert "--no-deps" not in cmd
    assert "--force-reinstall" not in cmd
```

- [x] **Step 2: Run test to verify it fails**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k wheel_install -v`
Expected: FAIL — base class raises a capability error.

- [x] **Step 3: Implement `wheel_install_command`**

Add to `SbxSandboxLauncher`:

```python
    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Full dependency install of the shipped wheels into the user site.

        Unlike the prebaked-host-image launchers, sbx's default image has
        no baked omnigent, so this installs WITH dependencies (no
        ``--no-deps`` / ``--force-reinstall``). ``--user`` lands the
        install where the bootstrap's PATH-persistence step adds to PATH
        (``$HOME/.local/bin``). ``--break-system-packages`` tolerates a
        PEP-668 externally-managed base python (drop it if the discovery
        task showed PEP 668 is not in force).

        :param remote_tgz_path: Sandbox path of the shipped tarball.
        :returns: Shell command string for :meth:`run`.
        """
        return (
            "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
            f"tar xzf {remote_tgz_path} -C oa-wheels --warning=no-unknown-keyword && "
            "pip install --user --break-system-packages "
            "--no-warn-script-location oa-wheels/*.whl"
        )
```

- [x] **Step 4: Run test to verify it passes**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k wheel_install -v`
Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): full-install wheel_install_command for default image"
```

---

### Task 7: `exec_foreground()` — hold `omnigent host` open with env injection

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `_sbx_binary`, `_resolve_env` (Task 2)
- Produces: `exec_foreground(self, sandbox_id: str, command: str) -> int`

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── exec_foreground ─────────────────────────────────────────


def test_exec_foreground_argv_and_env(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Foreground attach uses an interactive TTY, injects passthrough env via
    `-e`, forces TERM, and `exec`s the command so its exit code is returned.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=7)
    rc = SbxSandboxLauncher(env=["ANTHROPIC_API_KEY"]).exec_foreground(
        "box", "omnigent host --server u"
    )
    assert rc == 7
    [call] = fake_sbx.calls
    assert call.args[1:4] == ["exec", "-it", "-e"]
    assert call.args[4] == "ANTHROPIC_API_KEY=sk-test"
    assert call.args[5] == "box"
    assert call.args[6:9] == ["bash", "-lc"]
    assert call.args[9] == "TERM=xterm-256color exec omnigent host --server u"
    # Foreground inherits the terminal — output is NOT captured.
    assert call.capture is False


def test_exec_foreground_reraises_keyboard_interrupt(fake_sbx: _FakeSbx) -> None:
    """Ctrl-C during the attach tears down and re-raises."""
    fake_sbx.raise_on["exec"] = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        SbxSandboxLauncher().exec_foreground("box", "omnigent host --server u")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k exec_foreground -v`
Expected: FAIL — `exec_foreground` not implemented.

- [x] **Step 3: Implement `exec_foreground`**

Add to `SbxSandboxLauncher`:

```python
    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox over an interactive TTY with stdio
        inherited from the local terminal, returning its exit code.

        Passthrough env (resolved from :meth:`_resolve_env`) is injected
        with ``-e``; ``TERM`` is forced (native harnesses spawn tmux,
        which refuses a dumb/unset TERM); ``exec`` replaces the shell so
        the returned code is the command's own. The ``sbx`` process is a
        local child, so Ctrl-C reaches it directly — no remote-kill dance.

        :param sandbox_id: Target sandbox name.
        :param command: Shell command, e.g. ``"omnigent host --server …"``.
        :returns: The command's exit code.
        :raises KeyboardInterrupt: Re-raised when the user detaches.
        """
        argv = [self._sbx_binary(), "exec", "-it"]
        for name, value in self._resolve_env().items():
            argv += ["-e", f"{name}={value}"]
        argv += [sandbox_id, "bash", "-lc", f"TERM=xterm-256color exec {command}"]
        try:
            result = subprocess.run(argv, check=False)
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            raise
        return result.returncode
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k exec_foreground -v`
Expected: PASS (2 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): exec_foreground attach with env injection"
```

---

### Task 8: `attach()`, `keep_alive()`, `terminate()` — lifecycle

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `_run_sbx` (Task 2)
- Produces: `_sandbox_exists(self, sandbox_id: str) -> bool`; `attach`, `keep_alive`, `terminate` (all `(self, sandbox_id: str) -> None`)

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── attach / keep_alive / terminate ─────────────────────────


def _ls_json(*names: str) -> str:
    import json

    return json.dumps([{"name": n} for n in names])


def test_attach_ok_when_present(fake_sbx: _FakeSbx) -> None:
    """attach succeeds when the sandbox appears in `sbx ls --json`."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("box"))
    SbxSandboxLauncher().attach("box")  # must not raise


def test_attach_unknown_fails_with_hint(fake_sbx: _FakeSbx) -> None:
    """attach to a missing sandbox names the id."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("other"))
    with pytest.raises(click.ClickException, match="box"):
        SbxSandboxLauncher().attach("box")


def test_keep_alive_issues_no_sbx_call(fake_sbx: _FakeSbx) -> None:
    """
    sbx DOES idle-stop (~30s after the last session disconnects), but
    there is no disable knob — the foreground `connect` session is what
    holds the host up — so keep_alive is informational and calls no sbx.
    """
    SbxSandboxLauncher().keep_alive("box")
    assert fake_sbx.calls == []


def test_terminate_removes_when_present(fake_sbx: _FakeSbx) -> None:
    """terminate removes an existing sandbox via `sbx rm -f`."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json("box"))
    SbxSandboxLauncher().terminate("box")
    rm = fake_sbx.calls[-1]
    assert rm.args[1:] == ["rm", "-f", "box"]


def test_terminate_idempotent_when_absent(fake_sbx: _FakeSbx) -> None:
    """terminate is a no-op success when the sandbox is already gone."""
    fake_sbx.responses["ls"] = _FakeCompleted(args=[], stdout=_ls_json())
    SbxSandboxLauncher().terminate("box")
    assert [c.args[1] for c in fake_sbx.calls] == ["ls"]
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "attach or keep_alive or terminate" -v`
Expected: FAIL — methods not implemented.

- [x] **Step 3: Implement the lifecycle methods**

Add `import json` to the module imports, then add to `SbxSandboxLauncher`:

```python
    def _sandbox_exists(self, sandbox_id: str) -> bool:
        """Return whether a sandbox named *sandbox_id* is listed."""
        result = self._run_sbx(["ls", "--json"])
        if result.returncode != 0:
            raise click.ClickException(
                f"Could not list sbx sandboxes: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            entries = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            entries = []
        return any(entry.get("name") == sandbox_id for entry in entries)

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is present (``sbx exec``
        auto-starts a stopped one, so no explicit start is needed).

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When it does not exist.
        """
        click.echo(f"▸ Reusing existing sbx sandbox '{sandbox_id}'")
        if not self._sandbox_exists(sandbox_id):
            raise click.ClickException(
                f"sbx sandbox '{sandbox_id}' not found — create one with "
                "`omnigent sandbox create --provider sbx`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Informational: issues no sbx call. Discovery confirmed sbx DOES
        idle-stop a sandbox ~30s after its last session disconnects, but
        there is no "disable idle-stop" knob to set here. The host stays
        up the way it must anyway — the bootstrap's ``connect`` step runs
        ``omnigent host`` in the FOREGROUND via ``sbx exec``, which holds
        a session attached for the host's whole lifetime. A sandbox that
        is merely created but not yet connected may idle-stop; that is
        harmless because ``sbx exec`` auto-starts a stopped sandbox on the
        next call (``run`` during ship, ``exec_foreground`` on connect).

        :param sandbox_id: Unused; present to satisfy the contract.
        """
        click.echo(
            "  → sbx idle-stops a sandbox ~30s after its last session "
            "disconnects; `connect` holds one open. A stopped sandbox is "
            "auto-started on the next exec."
        )

    def terminate(self, sandbox_id: str) -> None:
        """
        Remove a sandbox via ``sbx rm -f``. Idempotent: an already-gone
        sandbox is treated as success.

        :param sandbox_id: The sandbox to remove.
        :raises click.ClickException: When removal of a present sandbox
            fails.
        """
        if not self._sandbox_exists(sandbox_id):
            return
        result = self._run_sbx(["rm", "-f", sandbox_id])
        if result.returncode != 0:
            raise click.ClickException(
                f"Could not remove sbx sandbox '{sandbox_id}': "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "attach or keep_alive or terminate" -v`
Expected: PASS (5 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): attach/keep_alive/terminate lifecycle"
```

---

### Task 9: Register the provider + capability-surface test

**Files:**
- Modify: `omnigent/onboarding/sandboxes/__init__.py:57-61` (the `_LAUNCHERS` dict)
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `available_providers`, `get_launcher` (`__init__.py`)
- Produces: `"sbx"` entry in `_LAUNCHERS`

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── registration / capability surface ───────────────────────


def test_provider_is_registered() -> None:
    """The sbx provider is listed and resolvable."""
    from omnigent.onboarding.sandboxes import available_providers, get_launcher

    assert "sbx" in available_providers()
    assert isinstance(get_launcher("sbx"), SbxSandboxLauncher)


def test_login_primitives_are_capability_gated(fake_sbx: _FakeSbx) -> None:
    """No local->sandbox forward path, so the OAuth primitives stay gated."""
    from omnigent.onboarding.sandboxes.base import SandboxCapabilityError

    launcher = SbxSandboxLauncher()
    with pytest.raises(SandboxCapabilityError):
        launcher.forward_local_port("box", 8022)
    with pytest.raises(SandboxCapabilityError):
        launcher.stream_exec("box", "echo hi")
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "registered or capability_gated" -v`
Expected: FAIL — `"sbx" not in available_providers()`.

- [x] **Step 3: Register the provider**

In `omnigent/onboarding/sandboxes/__init__.py`, add the `sbx` entry to `_LAUNCHERS`:

```python
_LAUNCHERS: dict[str, str] = {
    "lakebox": "omnigent.onboarding.sandboxes.lakebox:LakeboxLauncher",
    "modal": "omnigent.onboarding.sandboxes.modal:ModalSandboxLauncher",
    "daytona": "omnigent.onboarding.sandboxes.daytona:DaytonaSandboxLauncher",
    "sbx": "omnigent.onboarding.sandboxes.sbx:SbxSandboxLauncher",
}
```

- [x] **Step 4: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "registered or capability_gated" -v`
Expected: PASS (2 tests).

- [x] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/__init__.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): register sbx sandbox provider"
```

---

### Task 10: CLI `--kit` option + `get_launcher` threading

**Files:**
- Modify: `omnigent/onboarding/sandboxes/__init__.py:85-120` (`get_launcher` signature + sbx special-case)
- Modify: `omnigent/cli_sandbox.py` (add `--kit` to `sandbox_create`, thread to `get_launcher`)
- Test: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- Consumes: `get_launcher` (Task 9 registration), `SbxSandboxLauncher(kits=…)` (Task 2)
- Produces: `get_launcher(provider, *, workspace_host=None, kits=None)`; `--kit` repeatable option on `omnigent sandbox create`

- [x] **Step 1: Write the failing tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── CLI / get_launcher kit threading ────────────────────────


def test_get_launcher_threads_kits() -> None:
    """get_launcher passes kits into the sbx launcher constructor."""
    from omnigent.onboarding.sandboxes import get_launcher

    launcher = get_launcher("sbx", kits=("/tmp/sbxkit/claude",))
    assert isinstance(launcher, SbxSandboxLauncher)
    assert launcher._resolve_kits() == ["/tmp/sbxkit/claude"]


def test_create_command_forwards_kit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`omnigent sandbox create --provider sbx --kit X` forwards X to get_launcher."""
    from click.testing import CliRunner

    import omnigent.cli_sandbox as cli_sandbox

    recorded: dict[str, object] = {}

    def fake_get_launcher(provider: str, *, workspace_host=None, kits=None):
        recorded["provider"] = provider
        recorded["kits"] = kits
        return SbxSandboxLauncher(kits=kits)

    monkeypatch.setattr(cli_sandbox, "get_launcher", fake_get_launcher)
    monkeypatch.setattr(cli_sandbox, "_resolve_repo_root", lambda r: Path("/repo"))
    # Keep the command offline: stub workspace derivation + the bootstrap.
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.derive_workspace", lambda url: None
    )
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.bootstrap_sandbox_host",
        lambda *a, **k: "omnigent-host",
    )

    result = CliRunner().invoke(
        cli_sandbox.sandbox_create,
        [
            "--provider",
            "sbx",
            "--server",
            "https://example.com",
            "--kit",
            "/tmp/sbxkit/claude",
        ],
    )
    assert result.exit_code == 0, result.output
    assert recorded["provider"] == "sbx"
    assert recorded["kits"] == ("/tmp/sbxkit/claude",)
```

- [x] **Step 2: Run tests to verify they fail**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "threads_kits or forwards_kit" -v`
Expected: FAIL — `get_launcher` has no `kits` param / `--kit` option unknown.

- [x] **Step 3: Add the `kits` param + sbx special-case to `get_launcher`**

In `omnigent/onboarding/sandboxes/__init__.py`, update `get_launcher`:

```python
def get_launcher(
    provider: str,
    *,
    workspace_host: str | None = None,
    kits: Sequence[str] | None = None,
) -> SandboxLauncher:
```

Add `from collections.abc import Sequence` under `from __future__ import annotations`. Inside the function, after the existing lakebox special-case, before the generic `module_name, _, class_name = target.partition(":")` line, add:

```python
    if provider == "sbx":
        from omnigent.onboarding.sandboxes.sbx import SbxSandboxLauncher

        return SbxSandboxLauncher(kits=kits)
```

Update the `get_launcher` docstring with a one-line note that `kits` is consumed only by the `sbx` provider (other providers ignore it).

- [x] **Step 4: Add the `--kit` option to `sandbox create`**

In `omnigent/cli_sandbox.py`, add an option decorator to `sandbox_create` (after `--name`):

```python
@click.option(
    "--kit",
    "kits",
    multiple=True,
    help="sbx kit reference to apply at provision (repeatable). Only used by --provider sbx.",
)
```

Add `kits: tuple[str, ...]` to the `sandbox_create` signature, and pass it through where the launcher is built:

```python
    launcher = get_launcher(
        provider,
        workspace_host=workspace.host if workspace is not None else None,
        kits=kits,
    )
```

- [x] **Step 5: Run tests to verify they pass**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -k "threads_kits or forwards_kit" -v`
Expected: PASS (2 tests).

- [x] **Step 6: Run the full launcher test module + lint**

Run: `pytest tests/onboarding/sandboxes/test_sbx.py -v`
Expected: PASS (all tests).
Run: `srt -- ruff check omnigent/onboarding/sandboxes/sbx.py omnigent/cli_sandbox.py omnigent/onboarding/sandboxes/__init__.py` (or the repo's configured linter).
Expected: clean.

- [x] **Step 7: Commit**

```bash
git add omnigent/onboarding/sandboxes/__init__.py omnigent/cli_sandbox.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): wire --kit option through to the sbx launcher"
```

---

### Task 11: Docs + end-to-end manual verification

**Files:**
- Modify: `README.md` (the cloud-sandbox / providers section)
- Modify: `omnigent/cli_sandbox.py` (the `sandbox` group docstring "Provider notes")

- [x] **Step 1: Document the provider in the CLI help**

In `omnigent/cli_sandbox.py`, extend the `sandbox` group docstring's "Provider notes" block with:

```
      sbx      Local Docker Sandbox microVM (no cloud account). Needs the
               `sbx` CLI (https://docs.docker.com/ai/sandboxes/) + `sbx
               login`. Bind-mounts the current directory; each sandbox has
               its own Docker daemon for nested dev containers. Apply
               personal config with `--kit <ref>` (repeatable).
```

- [x] **Step 2: Document the provider in the README**

In `README.md`, in the "Run agents in cloud sandboxes" area, add a sentence noting a local option: running a session inside a local Docker Sandbox (`sbx`) microVM via `omnigent sandbox create --provider sbx`, with optional `--kit` config and nested-Docker support. Match the surrounding prose style.

- [x] **Step 3: Commit the docs**

```bash
git add README.md omnigent/cli_sandbox.py
git commit -m "docs(sbx): document the sbx sandbox provider"
```

- [x] **Step 4: End-to-end manual verification (real `sbx`, logged in, server reachable)**

Run, from an omnigent checkout, against a running Omnigent server URL:

```bash
git clone https://github.com/landreville/sbxkit /tmp/sbxkit   # one-time, for the kit
omnigent sandbox create --provider sbx --server <SERVER_URL> --kit /tmp/sbxkit/claude
```

Expected: sandbox is created, wheels build + ship + install succeed, the "Sandbox ready" banner prints a `connect` command.

```bash
omnigent sandbox connect --provider sbx --sandbox-id omnigent-host --server <SERVER_URL>
```

Expected: `omnigent host` runs in the sandbox and registers; the host appears in the server UI/TUI. Then verify in a session on that host:
- A claude-native session responds and is controllable like any other.
- A terminal (`sys_terminal_*`) opens and runs commands.
- `docker run --rm hello-world` works **inside** the sandbox (nested Docker).
- Ctrl-C on the `connect` foreground detaches cleanly.

```bash
sbx rm -f omnigent-host   # cleanup
```

- [x] **Step 5: Record verification results**

Note any deviations from the discovery-task assumptions (PEP 668 flag, setup deps, idle-stop). If `wheel_install_command` or `_SETUP_COMMAND` needed adjustment, the change should already be committed under the relevant task; otherwise fix and commit now.

---

## Self-Review

**Spec coverage:**
- New module + registration → Tasks 2, 9. ✓
- Class vars (`provider`/`supports_*`/`wheel_build_index_url`) → Task 2. ✓
- Constructor + env-var fallbacks (`template`/`env`/`kits`) → Task 2. ✓
- `prepare` (binary + login) → Task 3. ✓
- `provision` (create + kits + template + setup) → Task 4. ✓
- `run` / `put` → Task 5. ✓
- `wheel_install_command` full install → Task 6. ✓
- `exec_foreground` + credentials env passthrough → Task 7. ✓
- `attach` / `keep_alive` / `terminate` → Task 8. ✓
- Nested Docker + terminals (inherent / via host) → verified in Task 11. ✓
- Kit passthrough + CLI `--kit` + `get_launcher` special-case → Tasks 4, 10. ✓
- Error handling (ClickException with stderr) → every primitive task. ✓
- Testing (mocked sbx, argv asserts) → every task. ✓
- Open verification items → Task 1 (discovery), confirmed in Task 11. ✓

**Placeholder scan:** No "TBD"/"similar to Task N"/"add error handling" — every code step shows complete code and every test step shows real assertions. Task 1 is a legitimate discovery task with concrete commands, not a placeholder.

**Type consistency:** `provision` returns `str` (the name) consumed as `sandbox_id` everywhere; `_run_sbx` returns `subprocess.CompletedProcess[str]` used consistently; `_resolve_env`/`_resolve_kits`/`_resolve_template` signatures match their call sites in `provision`/`exec_foreground`; `get_launcher(..., kits=...)` matches the `cli_sandbox` call and the `SbxSandboxLauncher(kits=...)` constructor. Fake `subprocess.run` signature (`capture_output`/`text`/`check`) matches `_run_sbx` and `exec_foreground` call sites.
