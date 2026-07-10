# Server-managed `sbx` sandboxes from the Web UI — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` (or `superpowers:subagent-driven-development`) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Follow the `stacked-branches` skill: keep each branch releasable and test-passing, and only stack when a later branch literally cannot compile without the earlier one.

**Goal:** Let a user create a new `sbx` (Docker Sandbox microVM) instance directly from the Omnigent Web UI's new-session flow, alongside picking an already-connected sbx host. We do this by making `sbx` a managed-launch provider, reusing the existing `host_type="managed"` pipeline.

**Architecture:** `SbxSandboxLauncher` grows a managed mode (constructor `server_url` flag + `can_resume = True`). The server wiring in `omnigent/server/managed_hosts.py` adds `sbx` to the supported/managed provider sets and parses a `sandbox.sbx` block (`template`, `env`, `kits`). The prebaked image is built `FROM docker/sandbox-templates:shell-docker` with omnigent layered on top. The Web UI only needs a provider-name entry.

**Spec:** `docs/superpowers/specs/2026-07-08-sbx-managed-web-ui-design.md`

**Tech Stack:** Python 3.12+, `click`, `subprocess`, `sbx` CLI, pytest; TypeScript/React for the one-line label change.

## Global Constraints

- Python 3.12+ (`from __future__ import annotations` at the top of every new/modified Python module).
- The existing CLI-bootstrap sbx flow (`omnigent sandbox create --provider sbx`) must keep working unchanged.
- All launcher failures surface as `click.ClickException` with a remediation hint.
- No new third-party Python dependency — `sbx` is an external binary.
- One managed provider per server (`sandbox.provider` is a single value); enabling managed-sbx means the server's managed option *is* sbx.
- Follow TDD: write the failing test, watch it fail, implement minimally, watch it pass, commit.
- Run `pre-commit run --all-files` before each PR and fix what it reports.

## Branching Strategy

This feature naturally splits into three branches. Branch B depends on Branch A's launcher changes.

- **Branch A: `feature/sbx-managed-launcher`** — `SbxSandboxLauncher` managed mode (Task 1).
- **Branch B: `feature/sbx-managed-server`** — server wiring + Web UI label (Tasks 2–3). Stacks on A.
- **Branch C: `feature/sbx-host-image`** — `deploy/sbx/` Dockerfile + README (Task 4). Independent sibling off trunk; can land before or after A/B, but the live smoke test (Task 6) needs it published.

---

### Task 0: Discovery spike — resolve the runtime unknowns against a real `sbx`

This task produces **recorded findings** that confirm or adjust branches in the implementation. It requires a machine with `sbx` installed, Docker running, `sbx login` completed, and network access to pull `docker/sandbox-templates:shell-docker`.

**Files:**
- Modify: `docs/superpowers/specs/2026-07-08-sbx-managed-web-ui-design.md` (fill in the five discovery items)

- [x] **Step 1: Does `sbx create … shell` accept no host PATH?**

```bash
sbx create --name oa-path-probe shell
```

**Finding:** fails with `requires at least 1 argument: PATH`. Managed provision must pass a throwaway empty directory (e.g. `/tmp/omnigent-sbx-empty`).

- [x] **Step 2: Does a `setsid nohup` background survive `sbx exec` returning?**

```bash
sbx create --name oa-bg-probe shell .
sbx exec oa-bg-probe bash -lc 'setsid nohup sh -c "sleep 60" >/tmp/bg.log 2>&1 </dev/null & echo launched'
# wait a few seconds
sbx exec oa-bg-probe pgrep -a sleep
sbx rm -f oa-bg-probe
```

**Finding:** the sleep process survives. The default `run_background` works unchanged.

- [x] **Step 3: Is `sbx policy allow network` for the server host:port sufficient for dial-back?**

Created a sandbox, allowed only the server URL, and ran `curl` from inside.

**Finding:** `sbx policy allow network` works, but `host.docker.internal` resolves inside the sandbox to an IPv6 link-local address (`fe80::1`) that is not usable. Use the host's Docker bridge IP (e.g. `172.17.0.1`) in `sandbox.server_url` and allow that IP:port.

- [x] **Step 4: Does `sbx create -t <registry-ref>` accept an arbitrary OCI image, and does an omnigent layer on `shell-docker` keep nested Docker working?**

Built a probe image `FROM docker/sandbox-templates:shell-docker` and tried:

```bash
sbx create --name oa-img-probe -t <your-registry>/omnigent-host-sbx:probe shell
```

**Finding:** `sbx create -t` only accepts **registry-pullable** images; local-only tags fail with a pull error. The base `shell-docker` image already has a working nested Docker daemon, and adding layers preserves it as long as the Dockerfile does not override the base's entrypoint/CMD. The base runs as a non-root user, so `USER root` is required before installing packages.

- [x] **Step 5: Exact resume mechanics.**

```bash
sbx create --name oa-resume-probe shell .
sbx stop oa-resume-probe
sbx exec oa-resume-probe bash -lc 'echo awake'
sbx rm -f oa-resume-probe
```

**Finding:** `sbx exec` auto-starts a stopped sandbox. There is no `sbx start` subcommand, so `resume()` only needs to verify the sandbox exists.

- [x] **Step 6: `sbx ls --json` output shape.**

```bash
sbx ls --json
```

**Finding:** the current CLI returns `{"sandboxes": [...]}`, not a bare list. `_sandbox_exists` must parse the object wrapper.

- [ ] **Step 7: Update the spec and plan with the findings, then commit.**

```bash
git add docs/superpowers/specs/2026-07-08-sbx-managed-web-ui-design.md \
       docs/superpowers/plans/2026-07-08-sbx-managed-web-ui.md
git commit -m "docs(sbx): record managed-mode discovery findings"
```

---

### Task 1: Launcher managed-mode primitives

**Files:**
- Modify: `omnigent/onboarding/sandboxes/sbx.py`
- Modify: `tests/onboarding/sandboxes/test_sbx.py`

**Interfaces:**
- `SbxSandboxLauncher.__init__` gains `server_url: str | None = None`. When set, the launcher operates in managed mode.
- New class var `can_resume: ClassVar[bool] = True`.
- New helper `_managed: bool` (property) and `_exec_env_args() -> list[str]` for `sbx exec -e` injection in managed mode.
- `run()` injects configured env in managed mode.
- `provision()` branches: CLI-bootstrap path unchanged; managed path creates from template, bind-mounts a throwaway empty directory (the CLI requires a PATH argument), skips the install step, and allows server egress.
- `resume()` verifies the sandbox exists; `sbx exec` auto-starts it on the next command.
- `_sandbox_exists()` is updated to parse the current `sbx ls --json` object wrapper (`{"sandboxes": [...]}`) as well as the legacy bare list.

- [ ] **Step 1: Write the failing managed-mode tests**

Append to `tests/onboarding/sandboxes/test_sbx.py`:

```python
# ── managed mode ────────────────────────────────────────────


def test_managed_mode_class_vars() -> None:
    """Managed sbx hosts can resume in place."""
    assert SbxSandboxLauncher.can_resume is True


def test_managed_provision_argv_and_egress(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Managed provision creates from the template, bind-mounts a throwaway
    empty directory (the CLI requires a PATH), skips the setup step, and
    opens egress to the server + Claude domains.
    """
    empty = tmp_path / "empty"
    monkeypatch.setattr(sbxmod, "_MANAGED_EMPTY_WORKSPACE", empty)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["ANTHROPIC_API_KEY"],
        kits=["/opt/sbxkits/claude"],
        server_url="http://172.17.0.1:6767",
    )
    sandbox_id = launcher.provision("managed-box")

    assert sandbox_id == "managed-box"
    assert empty.exists()
    create, policy = fake_sbx.calls[:2]
    assert create.args[1:] == [
        "create",
        "--name",
        "managed-box",
        "--kit",
        "/opt/sbxkits/claude",
        "-t",
        "ghcr.io/me/omnigent-host-sbx:latest",
        "shell",
        str(empty),
    ]
    assert policy.args[1:5] == ["policy", "allow", "network", "--sandbox"]
    assert policy.args[5] == "managed-box"
    allowed = policy.args[6]
    assert "172.17.0.1:6767" in allowed
    assert "console.anthropic.com:443" in allowed
    # No root setup exec.
    assert [c.args[1:4] for c in fake_sbx.calls] != [["exec", "-u", "root"]]


def test_managed_run_injects_env(fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch) -> None:
    """Managed run() forwards configured env as `sbx exec -e NAME=VALUE`."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["OPENAI_API_KEY"],
        server_url="http://srv.example.com",
    )
    result = launcher.run("box", 'printf %s "$HOME"')
    assert result.returncode == 0
    [call] = fake_sbx.calls
    assert call.args[1] == "exec"
    assert "-e" in call.args
    assert "OPENAI_API_KEY=sk-openai" in call.args


def test_managed_run_missing_env_fails_loud(
    fake_sbx: _FakeSbx, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured env name that is unset in the server env fails loud."""
    monkeypatch.delenv("GIT_TOKEN", raising=False)
    launcher = SbxSandboxLauncher(
        template="ghcr.io/me/omnigent-host-sbx:latest",
        env=["GIT_TOKEN"],
        server_url="http://srv.example.com",
    )
    with pytest.raises(click.ClickException, match="GIT_TOKEN"):
        launcher.run("box", "echo hi")


def test_cli_run_does_not_inject_env(fake_sbx: _FakeSbx) -> None:
    """CLI-bootstrap mode keeps the existing run() argv (no -e injection)."""
    fake_sbx.responses["exec"] = _FakeCompleted(args=[], returncode=0, stdout="/root\n")
    result = SbxSandboxLauncher().run("box", 'printf %s "$HOME"')
    assert result.stdout == "/root\n"
    assert fake_sbx.calls[0].args[1:] == ["exec", "box", "bash", "-lc", 'printf %s "$HOME"']
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
pytest tests/onboarding/sandboxes/test_sbx.py -k managed -v
```

Expected: FAIL — `server_url` param / managed path / `-e` injection do not exist.

- [ ] **Step 3: Implement the managed-mode launcher changes**

In `omnigent/onboarding/sandboxes/sbx.py`:

```python
from urllib.parse import urlparse

# ... inside SbxSandboxLauncher ...

    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        template: str | None = None,
        env: Sequence[str] | None = None,
        kits: Sequence[str] | None = None,
        server_url: str | None = None,
    ) -> None:
        """
        :param server_url: Public URL the in-sandbox host dials back to.
            When set, the launcher operates in **managed** mode (server
            provisions the box). When unset, it operates in CLI-bootstrap
            mode (`omnigent sandbox create/connect`).
        """
        self._template = template
        self._env_names = tuple(env) if env is not None else None
        self._kits = tuple(kits) if kits is not None else None
        self._server_url = server_url
        self._binary: str | None = None

    @property
    def _managed(self) -> bool:
        """True when this launcher is driven by the server's managed flow."""
        return self._server_url is not None

    def _exec_env_args(self) -> list[str]:
        """
        Build `sbx exec -e NAME=VALUE` args for configured env in managed mode.

        CLI-bootstrap mode returns an empty list so `run()` stays unchanged
        for the local `omnigent sandbox create/connect` path.
        """
        if not self._managed:
            return []
        args: list[str] = []
        for name, value in self._resolve_env().items():
            args += ["-e", f"{name}={value}"]
        return args
```

Update `provision` to branch:

```python
    def provision(self, name: str) -> str:
        if self._managed:
            return self._provision_managed(name)
        # existing CLI-bootstrap provision body, unchanged
        ...
```

Add the managed workspace constant near the other constants:

```python
_MANAGED_EMPTY_WORKSPACE: Path = Path(tempfile.gettempdir()) / "omnigent-sbx-managed-empty"
"""Throwaway empty directory passed to `sbx create ... shell` in managed mode.

The `sbx` CLI requires a PATH argument even when the image provides its own
workspace, so we bind-mount this empty directory. It is created on demand."""
```

Add the managed provision helper:

```python
    def _provision_managed(self, name: str) -> str:
        """
        Create a managed sbx sandbox from the prebaked template.

        Bind-mounts an empty server-side directory (the CLI requires a PATH),
        skips the runtime setup step (the template already carries omnigent),
        and opens egress to the Omnigent server so the in-box host can register.
        """
        template = self._resolve_template()
        if not template:
            raise click.ClickException(
                "sbx managed mode requires a template image — set "
                "'sandbox.sbx.template' in the server config."
            )
        _MANAGED_EMPTY_WORKSPACE.mkdir(parents=True, exist_ok=True)
        args = ["create", "--name", name]
        for kit in self._resolve_kits():
            args += ["--kit", kit]
        args += ["-t", template, "shell", str(_MANAGED_EMPTY_WORKSPACE)]

        click.echo(f"▸ Creating managed sbx sandbox '{name}'")
        result = self._run_sbx(args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if "default network policy" in detail.lower():
                raise click.ClickException(
                    "sbx has no default network policy configured. Run "
                    "`sbx policy set-default balanced` (allows AI services + "
                    "package registries; use `allow-all` if your --server host "
                    "gets blocked), then retry."
                )
            raise click.ClickException(f"sbx sandbox creation failed: {detail}")

        self._allow_managed_egress(name)
        click.echo(f"  → created {name}")
        return name

    def _allow_managed_egress(self, name: str) -> None:
        """Best-effort allow the in-box host to reach the server + Claude auth."""
        server_host_port = _server_host_port(self._server_url)
        domains = f"{server_host_port},{_CLAUDE_AUTH_DOMAINS}"
        policy = self._run_sbx(
            ["policy", "allow", "network", "--sandbox", name, domains]
        )
        if policy.returncode != 0:
            click.echo(
                "  → warning: could not allow managed egress "
                f"({policy.stderr.strip() or policy.stdout.strip()}); "
                "the host may not be able to register."
            )
```

Add the server-URL parser at module level:

```python
def _server_host_port(server_url: str | None) -> str:
    """Extract ``host:port`` from a URL for sbx network policy allow rules."""
    if not server_url:
        raise click.ClickException("sbx managed mode requires sandbox.server_url")
    parsed = urlparse(server_url)
    host = parsed.hostname
    if not host:
        raise click.ClickException(f"could not parse sandbox.server_url: {server_url}")
    if parsed.port:
        return f"{host}:{parsed.port}"
    default_port = "443" if parsed.scheme == "https" else "80"
    return f"{host}:{default_port}"
```

Update `run()` to inject env in managed mode:

```python
    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        result = self._run_sbx(
            ["exec", *self._exec_env_args(), sandbox_id, "bash", "-lc", command]
        )
        ...
```

Add `resume()`:

```python
    def resume(self, sandbox_id: str) -> None:
        """
        Verify a managed sandbox still exists so the next `exec` wakes it.

        sbx retains the sandbox filesystem across idle-stop and auto-starts
        on the next `exec`, so `start_host` will bring the compute back.
        """
        if not self._sandbox_exists(sandbox_id):
            raise click.ClickException(
                f"sbx sandbox '{sandbox_id}' not found — cannot resume"
            )
        click.echo(f"  → sandbox '{sandbox_id}' exists; exec will auto-start it")
```

Update `_sandbox_exists()` to handle the current CLI output shape:

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
            parsed = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            parsed = {}
        # Current sbx CLI wraps the list in {"sandboxes": [...]}; older builds
        # returned a bare list. Accept both.
        entries = parsed if isinstance(parsed, list) else parsed.get("sandboxes", [])
        return any(entry.get("name") == sandbox_id for entry in entries)
```

- [ ] **Step 4: Run the launcher tests**

```bash
pytest tests/onboarding/sandboxes/test_sbx.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/sbx.py tests/onboarding/sandboxes/test_sbx.py
git commit -m "feat(sbx): managed-mode launcher primitives (template, egress, env, resume)"
```

---

### Task 2: Server wiring — make `sbx` a managed-launch provider

**Files:**
- Modify: `omnigent/server/managed_hosts.py`
- Modify: `tests/server/helpers.py`
- Modify: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Add `"sbx"` to `SUPPORTED_SANDBOX_PROVIDERS` and `PROVIDERS_WITH_MANAGED_LAUNCH`.
- Add `SBX_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600`.
- Add `_sbx_launcher_factory(template, env, kits, server_url)`.
- Add `_parse_sbx_template`, `_parse_sbx_env`, `_parse_sbx_kits`.
- Wire the `elif provider == "sbx":` branch in `parse_sandbox_config`.
- Add `install_fake_sbx_launcher` test helper.

- [ ] **Step 1: Write the failing server-config tests**

In `tests/server/helpers.py`, add after the existing install helpers:

```python
def install_fake_sbx_launcher(
    monkeypatch: Any,
    fake: FakeSandboxLauncher,
) -> None:
    """Substitute the fake for ``SbxSandboxLauncher`` at its public seam."""
    import omnigent.onboarding.sandboxes.sbx as sbx_mod

    def _ctor(
        *,
        template: str | None = None,
        env: list[str] | None = None,
        kits: list[str] | None = None,
        server_url: str | None = None,
    ) -> FakeSandboxLauncher:
        fake.template = template
        fake.env = env
        fake.kits = kits
        fake.server_url = server_url
        fake.provider = "sbx"  # type: ignore[misc]
        return fake

    monkeypatch.setattr(sbx_mod, "SbxSandboxLauncher", _ctor)
```

In `tests/server/test_managed_hosts.py`, add to the imports:

```python
from omnigent.server.managed_hosts import SBX_MANAGED_TOKEN_TTL_S
from tests.server.helpers import install_fake_sbx_launcher
```

Append tests:

```python
def test_sbx_is_supported_and_managed() -> None:
    """sbx appears in both provider sets."""
    from omnigent.server.managed_hosts import (
        PROVIDERS_WITH_MANAGED_LAUNCH,
        SUPPORTED_SANDBOX_PROVIDERS,
    )

    assert "sbx" in SUPPORTED_SANDBOX_PROVIDERS
    assert "sbx" in PROVIDERS_WITH_MANAGED_LAUNCH


def test_parse_sbx_config_threads_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox.sbx template/env/kits/server_url reach the launcher factory."""
    cfg = parse_sandbox_config(
        {
            "provider": "sbx",
            "server_url": "http://172.17.0.1:6767/",
            "sbx": {
                "template": "ghcr.io/me/omnigent-host-sbx:latest",
                "env": ["ANTHROPIC_API_KEY", "GIT_TOKEN"],
                "kits": ["/opt/sbxkits/claude"],
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "http://172.17.0.1:6767"
    assert cfg.token_ttl_s == SBX_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "sbx"
    fake = FakeSandboxLauncher()
    install_fake_sbx_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.template == "ghcr.io/me/omnigent-host-sbx:latest"
    assert fake.env == ["ANTHROPIC_API_KEY", "GIT_TOKEN"]
    assert fake.kits == ["/opt/sbxkits/claude"]
    assert fake.server_url == "http://172.17.0.1:6767"


def test_parse_sbx_config_requires_template() -> None:
    """Managed sbx needs an explicit template; missing config fails loud."""
    with pytest.raises(ValueError, match=r"sandbox\.sbx\.template"):
        parse_sandbox_config(
            {
                "provider": "sbx",
                "server_url": "http://srv.example.com",
            }
        )
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
pytest tests/server/test_managed_hosts.py -k sbx -v
```

Expected: FAIL — imports/constants/helpers do not exist.

- [ ] **Step 3: Implement the server wiring**

In `omnigent/server/managed_hosts.py`:

1. Add `"sbx"` to `SUPPORTED_SANDBOX_PROVIDERS` and `PROVIDERS_WITH_MANAGED_LAUNCH`.
2. Add the TTL constant near the other provider TTLs:

```python
# Launch-token lifetime for the YAML sbx path. sbx sandboxes have no
# platform lifetime cap (they persist until deleted), so the bound is
# policy: 7 days keeps a live sandbox re-authenticating across tunnel
# reconnects while still expiring tokens of sandboxes nobody deleted.
SBX_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600
```

3. Add the parser helpers (near `_parse_provider_image` / `_parse_provider_env`):

```python
def _parse_sbx_template(raw: dict[str, object]) -> str:
    """
    Extract and validate ``sandbox.sbx.template`` — required for managed mode.

    The default sbx image has no omnigent, so a prebaked template is
    mandatory. A missing or malformed value stops server startup.
    """
    section = _parse_provider_section(raw, "sbx")
    if section is None or section.get("template") is None:
        raise ValueError(
            "server config 'sandbox.sbx.template' is required for provider 'sbx' — "
            "the prebaked omnigent-host image, e.g. "
            "'ghcr.io/you/omnigent-host-sbx:latest'"
        )
    template = section["template"]
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "server config 'sandbox.sbx.template' must be a registry image "
            "reference with omnigent pre-installed"
        )
    return template.strip()


def _parse_sbx_kits(raw: dict[str, object]) -> list[str] | None:
    """Extract and validate ``sandbox.sbx.kits`` — optional sbx kit references."""
    section = _parse_provider_section(raw, "sbx")
    if section is None:
        return None
    kits = section.get("kits")
    if kits is None:
        return None
    if not isinstance(kits, list) or not all(
        isinstance(k, str) and k.strip() for k in kits
    ):
        raise ValueError(
            "server config 'sandbox.sbx.kits' must be a list of sbx kit references"
        )
    return [k.strip() for k in kits]
```

`env` reuse `_parse_provider_env(raw, "sbx")`.

4. Add the factory (near `_daytona_launcher_factory`):

```python
def _sbx_launcher_factory(
    *,
    template: str,
    env: list[str] | None,
    kits: list[str] | None,
    server_url: str,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: sbx`` path.

    :param template: Registry image reference of the prebaked omnigent-host
        sbx image (required).
    :param env: Names of server-process environment variables injected into
        every managed sandbox, or ``None``.
    :param kits: sbx kit references applied at provision, or ``None``.
    :param server_url: Public URL the in-sandbox host dials back to.
    """

    def _build() -> SandboxLauncher:
        from omnigent.onboarding.sandboxes.sbx import SbxSandboxLauncher

        return SbxSandboxLauncher(
            template=template,
            env=env,
            kits=kits,
            server_url=server_url,
        )

    return _build
```

5. Wire the branch in `parse_sandbox_config`:

```python
    elif provider == "sbx":
        launcher_factory = _sbx_launcher_factory(
            template=_parse_sbx_template(raw),
            env=_parse_provider_env(raw, "sbx"),
            kits=_parse_sbx_kits(raw),
            server_url=server_url.strip().rstrip("/"),
        )
        token_ttl_s = SBX_MANAGED_TOKEN_TTL_S
```

6. Update the module docstring example `sandbox:` section to mention `sbx`.

- [ ] **Step 4: Run the server tests**

```bash
pytest tests/server/test_managed_hosts.py -k sbx -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/managed_hosts.py tests/server/helpers.py tests/server/test_managed_hosts.py
git commit -m "feat(sbx): wire sbx as a managed-launch provider in managed_hosts"
```

---

### Task 3: Web UI provider label

**Files:**
- Modify: `web/src/lib/capabilities.ts`
- Create: `web/src/lib/capabilities.test.ts`

**Interfaces:**
- `_SANDBOX_PROVIDER_NAMES["sbx"] = "Sbx"`.

- [ ] **Step 1: Add the label mapping**

In `web/src/lib/capabilities.ts`:

```typescript
const _SANDBOX_PROVIDER_NAMES: Record<string, string> = {
  modal: "Modal",
  lakebox: "Databricks",
  daytona: "Daytona",
  e2b: "E2B",
  sbx: "Sbx",
};
```

- [ ] **Step 2: Add a unit test**

Create `web/src/lib/capabilities.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import { sandboxOptionLabel } from "./capabilities";

describe("sandboxOptionLabel", () => {
  it("labels sbx as Sbx Sandbox", () => {
    expect(sandboxOptionLabel("sbx")).toBe("Sbx Sandbox");
  });

  it("falls back to title case for unknown providers", () => {
    expect(sandboxOptionLabel("future")).toBe("Future Sandbox");
  });

  it("returns generic label when provider is null", () => {
    expect(sandboxOptionLabel(null)).toBe("New Sandbox");
  });
});
```

- [ ] **Step 3: Run the web test**

```bash
cd web && npm run test -- capabilities.test.ts
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add web/src/lib/capabilities.ts web/src/lib/capabilities.test.ts
git commit -m "feat(web): label the managed sbx sandbox option"
```

---

### Task 4: Prebaked omnigent-host sbx image

**Files:**
- Create: `deploy/sbx/Dockerfile`
- Create: `deploy/sbx/README.md`

**Interfaces:**
- A registry-publishable image based on `docker/sandbox-templates:shell-docker` that bakes omnigent + harness CLIs + preserves the base's nested Docker daemon.

- [ ] **Step 1: Create `deploy/sbx/Dockerfile`**

Use a multi-stage build so build tools do not ship in the final image and the base's Docker-daemon entrypoint is preserved:

```dockerfile
# Omnigent host image for Docker Sandboxes (sbx).
#
# Built on Docker's own `shell-docker` sandbox template so the microVM
# retains the nested Docker daemon — the main reason to pick sbx over
# cloud providers. The image bakes omnigent + git/tmux/procps/lsof/
# bubblewrap + the coding-harness CLIs so managed launches skip the
# in-sandbox dependency install.
#
# Build & push:
#   docker build -t ghcr.io/you/omnigent-host-sbx:latest -f deploy/sbx/Dockerfile .
#   docker push ghcr.io/you/omnigent-host-sbx:latest

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=20
ARG PYPI_INDEX_URL=https://pypi.org/simple

# Node binaries to copy into the runtime stage.
FROM node:${NODE_VERSION}-slim AS node-runtime

# Builder stage: installs omnigent and harness tools on the sbx base.
FROM docker/sandbox-templates:shell-docker AS builder
# The base image runs as a non-root user by default; package installs need root.
USER root
ARG PYPI_INDEX_URL
ARG NPM_CONFIG_REGISTRY=
ARG KIRO_CLI_VERSION=2.10.0
ARG KIRO_CLI_SHA256_AMD64=be9d8b6d7c44f93a83ca22466043d98ad058e6ed3c12fffd068f3fb8a60b3b70
ARG KIRO_CLI_SHA256_ARM64=0afb37399b9e2847c2f2e3f5d9052c8bc52bbf1e30401ea284a602661bce34bc
ARG AGY_VERSION=1.0.10
ARG AGY_SHA256_AMD64=6547cf9a37227f26004fa4b805418b1df96f54c57b9723ca7d10864d2610bb0f
ARG AGY_SHA256_ARM64=4674fabc3681221e54c90d15077c9a97a25ea71222001dabe44bf1576e888593

ENV NPM_CONFIG_REGISTRY=${NPM_CONFIG_REGISTRY} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv build-essential \
      git tmux procps lsof bubblewrap curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

RUN python3 -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /build
COPY pyproject.toml setup.py LICENSE NOTICE ./
COPY sdks/ ./sdks/
COPY omnigent/ ./omnigent/
COPY examples/ ./examples/

RUN uv pip install --no-cache-dir --index-url ${PYPI_INDEX_URL} -e .

# Node runtime for the harness CLIs.
COPY --from=node-runtime /usr/local/bin/node /usr/local/bin/node
COPY --from=node-runtime /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

RUN npm install -g --no-audit --no-fund \
      @anthropic-ai/claude-code \
      @openai/codex \
      @earendil-works/pi-coding-agent \
 && npm cache clean --force

# Pin kiro-cli (keep in sync with deploy/docker/Dockerfile).
RUN set -eu; \
    case "$(uname -m)" in \
      x86_64)  asset="kirocli-x86_64-linux.zip";  sha="${KIRO_CLI_SHA256_AMD64}" ;; \
      aarch64) asset="kirocli-aarch64-linux.zip"; sha="${KIRO_CLI_SHA256_ARM64}" ;; \
      *) echo "ERROR: unsupported arch '$(uname -m)' for kiro-cli" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/kiro.zip "https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/${asset}"; \
    echo "${sha}  /tmp/kiro.zip" | sha256sum -c -; \
    unzip -q /tmp/kiro.zip -d /tmp/kiro; \
    KIRO_CLI_SKIP_SETUP=1 sh /tmp/kiro/kirocli/install.sh; \
    install -m 0755 /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli; \
    if [ -f /root/.local/bin/kiro-cli-chat ]; then \
      install -m 0755 /root/.local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat; \
    fi; \
    rm -rf /tmp/kiro /tmp/kiro.zip

# Pin agy (keep in sync with deploy/docker/Dockerfile).
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) asset="agy_cli_linux_x64.tar.gz"; sha="${AGY_SHA256_AMD64}" ;; \
      arm64) asset="agy_cli_linux_arm64.tar.gz"; sha="${AGY_SHA256_ARM64}" ;; \
      *) echo "ERROR: unsupported arch '$arch' for agy" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/agy.tar.gz "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/${asset}"; \
    echo "${sha}  /tmp/agy.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/agy.tar.gz -C /tmp antigravity; \
    install -m 0755 /tmp/antigravity /usr/local/bin/agy; \
    rm -f /tmp/agy.tar.gz /tmp/antigravity; \
    test -x /usr/local/bin/agy

# Final stage: start from the sbx base again so the Docker-daemon entrypoint
# is preserved. Only the runtime artifacts are copied in.
FROM docker/sandbox-templates:shell-docker
USER root

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    IS_SANDBOX=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 python3-venv git tmux procps lsof bubblewrap curl ca-certificates unzip \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /usr/local/bin/node /usr/local/bin/node
COPY --from=builder /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=builder /usr/local/bin/npm /usr/local/bin/npm
COPY --from=builder /usr/local/bin/npx /usr/local/bin/npx
COPY --from=builder /usr/local/bin/kiro-cli /usr/local/bin/kiro-cli
COPY --from=builder /usr/local/bin/kiro-cli-chat /usr/local/bin/kiro-cli-chat
COPY --from=builder /usr/local/bin/agy /usr/local/bin/agy
COPY --from=builder /build /build

# Git credential helper for private repos (same as deploy/docker/Dockerfile).
RUN git config --system credential.helper \
      '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; printf "username=%s\npassword=%s\n" "${GIT_USERNAME:-x-access-token}" "$GIT_TOKEN"; }; f'

# Keep the venv on PATH through login shells.
RUN echo 'export PATH="/opt/venv/bin:${PATH}"' > /etc/profile.d/omnigent-venv.sh

WORKDIR /root

# Do NOT set ENTRYPOINT or CMD; preserve the base image's daemon launcher.
```

- [ ] **Step 2: Create `deploy/sbx/README.md`**

Sections:

1. **What this image is** — prebaked Omnigent host for sbx microVMs.
2. **Prerequisites** — server box has `sbx` CLI, Docker, KVM, `sbx login`, default network policy.
3. **Build & publish** — `docker build ...`, `docker push ...`.
4. **Server config example**:

```yaml
sandbox:
  provider: sbx
  # Use the host's Docker bridge IP, not host.docker.internal — inside an sbx
  # sandbox host.docker.internal resolves to an unusable IPv6 link-local address.
  server_url: http://172.17.0.1:6767
  sbx:
    template: ghcr.io/you/omnigent-host-sbx:latest
    env: [ANTHROPIC_API_KEY, OPENAI_API_KEY, GIT_TOKEN]
    kits: [/opt/sbxkits/claude]
```

5. **Verification** — `sbx create -t <image> shell`, `docker run hello-world`, `omnigent host --server ...`.
6. **Troubleshooting** — network policy, Docker bridge IP vs `host.docker.internal`, template pull auth.

- [ ] **Step 3: Build and publish the image**

```bash
docker build -t ghcr.io/you/omnigent-host-sbx:latest -f deploy/sbx/Dockerfile .
docker push ghcr.io/you/omnigent-host-sbx:latest
```

This step needs a registry the sbx daemon can pull from. If the registry is private, configure `sbx` login / credential helper access.

- [ ] **Step 4: Commit**

```bash
git add deploy/sbx/
git commit -m "feat(sbx): prebaked omnigent-host image for Docker Sandboxes"
```

---

### Task 5: Full test pass + pre-commit

- [ ] **Step 1: Run the affected Python test suites**

```bash
pytest tests/onboarding/sandboxes/test_sbx.py tests/server/test_managed_hosts.py -q
```

Expected: all PASS.

- [ ] **Step 2: Run the web test suite**

```bash
cd web && npm run test -- capabilities.test.ts
```

Expected: PASS.

- [ ] **Step 3: Run pre-commit across the changed files**

```bash
pre-commit run --all-files
```

Fix anything it reports and re-commit.

---

### Task 6: Live integration smoke test

Requires Branch A/B/C merged (or checked out together) and a server with `sbx` installed.

- [ ] **Step 1: Configure the server**

Create or edit `<data_dir>/config.yaml`:

```yaml
sandbox:
  provider: sbx
  server_url: http://172.17.0.1:6767
  sbx:
    template: ghcr.io/you/omnigent-host-sbx:latest
    env: [ANTHROPIC_API_KEY]
```

Start the server (`omnigent server -c ...`).

- [ ] **Step 2: Confirm capability advertisement**

```bash
curl -s http://localhost:6767/v1/info | jq '.managed_sandboxes_enabled, .sandbox_provider'
```

Expected: `true` and `"sbx"`.

- [ ] **Step 3: Create a managed session from the Web UI**

Open the new-session composer, pick **Sbx Sandbox**, optionally enter a public repo URL, and send a message. The server should:

1. Provision a new sbx microVM.
2. Start `omnigent host` inside it.
3. Register the host and bind the session.
4. Run a turn.

- [ ] **Step 4: Verify nested Docker inside the sandbox**

From the server box:

```bash
sbx ls
sbx exec <managed-sandbox-name> bash -lc 'docker run --rm hello-world'
```

Expected: `hello-world` runs successfully.

- [ ] **Step 5: Verify idle-stop / resume**

Let the session idle until the host goes offline, then send another message. The server should call `resume()` and the host should re-register under the same sandbox id.

- [ ] **Step 6: Verify teardown**

Delete the session from the Web UI. The managed host row should be removed and the sbx sandbox should be deleted:

```bash
sbx ls
```

Expected: the managed sandbox is gone.

---

### Task 7: Open the pull request(s)

Use the repo's PR template (`.github/pull_request_template.md`) for each branch. Keep sections/checkboxes intact.

- **Branch A** (`feature/sbx-managed-launcher`): launcher managed-mode primitives.
- **Branch B** (`feature/sbx-managed-server`): server wiring + Web UI label (stacks on A).
- **Branch C** (`feature/sbx-host-image`): prebaked image (independent).

Include in each:

- **Summary** — what changed and why.
- **Test Plan** — commands run, tests passed.
- **Demo** — for Branch B/UI, a screenshot of the host picker showing "Sbx Sandbox"; for other branches, `N/A`.
- **Type of change** / **Test coverage** — check applicable boxes.
- **Coverage notes** — manual verification steps for the live smoke test.

Generate the description from the actual diff and this session's context; do not skip template sections.

(End of file)
