"""Provider-agnostic tests for the :class:`SandboxLauncher` base behavior.

The exec-model defaults (``run_background`` / ``start_host``) are shared by
every provider whose sandbox is a bare box the server execs into (Modal,
Daytona, E2B, Boxlite, Islo, …), so they are tested once here against a
minimal recording launcher rather than per provider.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
    render_host_config_write_command,
)


class _RecordingLauncher(SandboxLauncher):
    """Minimal exec-model launcher that records every ``run`` command."""

    provider: ClassVar[str] = "recording"

    def __init__(self, home: str = "/root") -> None:
        self.commands: list[str] = []
        self.backgrounded: list[str] = []
        self._home = home

    def prepare(self) -> None:  # pragma: no cover - unused preflight stub
        pass

    def provision(self, name: str) -> str:  # pragma: no cover - unused stub
        return "sb-1"

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        # start_host probes $HOME first; everything else returns empty.
        stdout = self._home if command == 'printf %s "$HOME"' else ""
        return RemoteCommandResult(returncode=0, stdout=stdout, stderr="")

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        # Capture the raw (pre-wrap) command so a test can prove a real shell
        # honors its env prefix, independent of the setsid/nohup wrapper.
        self.backgrounded.append(command)
        return super().run_background(sandbox_id, command, log_path=log_path)


def test_run_background_wraps_command_in_sh_c() -> None:
    """
    ``run_background`` must wrap the command in ``sh -c`` so env-var prefixes
    survive ``nohup``. ``nohup ENV=val cmd`` makes nohup try to exec a program
    literally named ``ENV=val`` ("No such file or directory") — re-parsing under
    ``sh -c`` lets the inner shell apply the assignment before running ``cmd``.
    Regression: managed Daytona/Modal hosts never came online because the
    in-sandbox ``omnigent host`` launch died on its ``OMNIGENT_HOST_TOKEN=…``
    prefix.
    """
    launcher = _RecordingLauncher()

    launcher.run_background("sb-1", "FOO=bar omnigent host --server https://srv")

    [cmd] = launcher.commands
    assert cmd == (
        "setsid nohup sh -c 'FOO=bar omnigent host --server https://srv' "
        "> /tmp/omnigent-host.log 2>&1 < /dev/null & echo launched"
    )


def test_start_host_env_prefix_is_honored_by_a_real_shell() -> None:
    """
    The env-prefixed command ``start_host`` hands to ``run_background`` must
    apply its ``OMNIGENT_HOST_*`` assignments when re-parsed by a shell — the
    exact thing the ``sh -c`` wrapper restores. Run the raw command through a
    real ``sh -c`` (the inner shell of the wrapper) with ``omnigent host``
    swapped for a probe that echoes the injected vars; the broken bare-``nohup``
    form would never reach this assignment-honoring shell.
    """
    launcher = _RecordingLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )
    assert workspace == "/root/workspace"

    [raw] = launcher.backgrounded
    # A nested `sh -c` reads the *inherited* env (a bare `$VAR` in the same
    # simple command would expand in the parent shell, before the temporary
    # assignment takes effect — and print empty).
    probe = raw.replace(
        "omnigent host --server https://srv",
        "sh -c 'printf %s:%s:%s "
        '"$OMNIGENT_HOST_TOKEN" "$OMNIGENT_HOST_ID" "$OMNIGENT_HOST_NAME"\'',
    )
    out = subprocess.run(
        ["sh", "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "tok-123:host_abc:managed-abc"


def test_start_host_default_materialize_clones_repo() -> None:
    """
    With a ``repo_url``, the default :meth:`materialize_workspace` clones into
    ``<workspace>/<repo_name>`` and ``start_host`` returns that checkout dir —
    the exec-model behavior every default provider (Modal/Daytona/E2B/…)
    inherits, unchanged by the extraction of the clone into its own method.
    """
    launcher = _RecordingLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
        repo_url="https://github.com/org/repo",
        repo_branch="release-1.2",
        repo_name="repo",
    )

    assert workspace == "/root/workspace/repo"
    assert (
        "git clone --branch release-1.2 --single-branch -- "
        "https://github.com/org/repo /root/workspace/repo"
    ) in launcher.commands


def test_materialize_workspace_override_resolves_local_checkout_without_cloning() -> None:
    """
    A provider whose sandbox already carries the repository overrides
    :meth:`materialize_workspace` to resolve the identity to a local path and
    performs NO clone. ``start_host`` must use the override's returned path and
    still launch the host — proving the seam lets a provider swap repo
    materialization without reimplementing ``start_host``.
    """

    class _LocalCheckoutLauncher(_RecordingLauncher):
        def materialize_workspace(
            self,
            sandbox_id: str,
            *,
            workspace: str,
            repo_url: str,
            repo_branch,
            repo_name,
            on_stage=None,
        ) -> str:
            # Resolve the repo identity to a pre-provisioned local checkout;
            # fetch the branch into it rather than cloning the URL.
            local = f"/checkouts/{repo_name}"
            if repo_branch is not None:
                self.run(sandbox_id, f"git -C {local} checkout {repo_branch}")
            return local

    launcher = _LocalCheckoutLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
        repo_url="https://github.com/org/repo",
        repo_branch="main",
        repo_name="repo",
    )

    assert workspace == "/checkouts/repo"
    # No clone happened; the override resolved a local checkout instead.
    assert not any(cmd.startswith("git clone") for cmd in launcher.commands)
    assert "git -C /checkouts/repo checkout main" in launcher.commands
    # The host still launched, in the resolved workspace.
    [raw] = launcher.backgrounded
    assert raw.endswith("omnigent host --server https://srv")


# ── host_config materialization ────────────────────────────

_GATEWAY_HOST_CONFIG: dict[str, object] = {
    "providers": {
        "litellm": {
            "kind": "gateway",
            "default": ["pi"],
            "openai": {
                "base_url": "http://litellm.litellm.svc.cluster.local/v1",
                "api_key_ref": "env:LITELLM_API_KEY",
                "wire_api": "chat",
            },
        }
    }
}


def _run_write_command(
    command: str,
    home: Path,
    *,
    config_home: Path | None = None,
    extra_env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the rendered write command through a real shell + python3."""
    env = {**os.environ, "HOME": str(home)}
    env.pop("OMNIGENT_CONFIG_HOME", None)
    if config_home is not None:
        env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    if extra_env is not None:
        env.update(extra_env)
    return subprocess.run(
        ["sh", "-c", command],
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _materialize(
    command: str, home: Path, *, config_home: Path | None = None
) -> dict[str, object]:
    """Run the command and return the config from its resolved directory."""
    _run_write_command(command, home, config_home=config_home)
    config_dir = config_home if config_home is not None else home / ".omnigent"
    with open(config_dir / "config.yaml") as f:
        return yaml.safe_load(f)


def test_render_host_config_write_command_creates_config_from_scratch(tmp_path: Path) -> None:
    """A fresh sandbox (no ~/.omnigent at all) gets the injected config verbatim."""
    written = _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)
    assert written == _GATEWAY_HOST_CONFIG


def test_render_host_config_write_command_honors_omnigent_config_home(
    tmp_path: Path,
) -> None:
    """The writer uses OMNIGENT_CONFIG_HOME as the config directory itself."""
    home = tmp_path / "home"
    home.mkdir()
    config_home = tmp_path / "custom-config"

    written = _materialize(
        render_host_config_write_command(_GATEWAY_HOST_CONFIG),
        home,
        config_home=config_home,
    )

    assert written == _GATEWAY_HOST_CONFIG
    assert (config_home / ".injected_host_config.json").exists()
    assert not (home / ".omnigent").exists()


def test_render_host_config_write_command_merges_providers_and_replaces_other_keys(
    tmp_path: Path,
) -> None:
    """
    The merge mirrors cli.py's ``deep_merge_keys=("providers",)``: sibling
    provider entries survive, an injected entry of the same name wins
    wholesale, other top-level keys replace, untouched keys persist.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {"kind": "key"},
                    "litellm": {"kind": "gateway", "default": True},
                },
                "server": "https://old.example.com",
                "host": {"name": "keep-me"},
            }
        )
    )

    injected = {**_GATEWAY_HOST_CONFIG, "server": "https://new.example.com"}
    written = _materialize(render_host_config_write_command(injected), tmp_path)

    providers = written["providers"]
    assert providers["anthropic"] == {"kind": "key"}  # sibling survives
    # Same-name entry replaced wholesale (no per-entry merge), injected wins.
    assert providers["litellm"] == _GATEWAY_HOST_CONFIG["providers"]["litellm"]
    assert written["server"] == "https://new.example.com"
    assert written["host"] == {"name": "keep-me"}


def test_render_host_config_write_command_survives_hostile_yaml_content(tmp_path: Path) -> None:
    """
    Quotes, ``$VAR``-looking strings, backticks, newlines, and unicode round-trip
    byte-exact: the payload rides base64 through the shell/python layers, so no
    operator YAML can break out of the quoting.
    """
    hostile: dict[str, object] = {
        "providers": {
            'we\'ird "name"': {
                "kind": "gateway",
                "note": "line1\nline2 `tick` $HOME 'single' — ünïcode ✓",
            }
        }
    }
    written = _materialize(render_host_config_write_command(hostile), tmp_path)
    assert written == hostile


def test_materialized_config_routes_pi_to_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The point of the injection: a host booted with the materialized config
    resolves the gateway as pi's provider through the REAL config loader and
    harness-routing chain — before any ambient env credential is consulted.
    """
    _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)

    from omnigent.onboarding.provider_config import default_provider_for_harness, load_config

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / ".omnigent"))
    entry = default_provider_for_harness(load_config(), "pi")

    assert entry is not None
    assert entry.name == "litellm"
    assert entry.kind == "gateway"


def test_start_host_writes_host_config_before_launching_the_host() -> None:
    """The config write runs via ``run`` strictly before the host is backgrounded."""
    launcher = _RecordingLauncher()

    launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
        host_config=_GATEWAY_HOST_CONFIG,
    )

    write_index = launcher.commands.index(render_host_config_write_command(_GATEWAY_HOST_CONFIG))
    # run_background funnels through run(), so the wrapped host launch is
    # also in `commands` — the write must precede it.
    host_index = next(
        i for i, cmd in enumerate(launcher.commands) if "omnigent host --server" in cmd
    )
    assert write_index < host_index


def test_start_host_without_host_config_writes_nothing() -> None:
    """No host_config on a fresh-sandbox launcher → no config command at all.

    Non-resumable sandboxes can't carry a stale injection marker, so the
    cleanup run would be dead weight (and a python3+yaml image requirement
    for operators who never use the feature).
    """
    launcher = _RecordingLauncher()

    launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )

    assert not any(cmd.startswith("python3 -c") for cmd in launcher.commands)


# ── server-managed replacement semantics ────────────────────


def _read_marker(home: Path) -> dict[str, object] | None:
    marker = home / ".omnigent" / ".injected_host_config.json"
    if not marker.exists():
        return None
    return json.loads(marker.read_text())


def test_render_host_config_write_command_replaces_previously_injected_entries(
    tmp_path: Path,
) -> None:
    """
    Renaming a gateway in ``sandbox.host_config`` must not leave the old
    entry behind: two providers claiming the same ``default`` scope is a
    load error inside the sandbox. Previously injected providers and
    top-level keys are removed before the current payload merges in;
    user-created entries survive.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"mine": {"kind": "key"}}})
    )
    first = {
        "providers": {"gateway_a": {"kind": "gateway", "default": ["pi"]}},
        "server": "https://old.example.com",
    }
    second = {"providers": {"gateway_b": {"kind": "gateway", "default": ["pi"]}}}

    _materialize(render_host_config_write_command(first), tmp_path)
    written = _materialize(render_host_config_write_command(second), tmp_path)

    assert written["providers"] == {
        "mine": {"kind": "key"},
        "gateway_b": {"kind": "gateway", "default": ["pi"]},
    }
    # The previously injected non-providers key is gone, not just replaced.
    assert "server" not in written
    assert _read_marker(tmp_path) == second


def test_render_host_config_write_command_empty_payload_removes_injected_config(
    tmp_path: Path,
) -> None:
    """Removing ``host_config`` from server config cleans up on the next run."""
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"name": "keep-me"}})
    )

    _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)
    written = _materialize(render_host_config_write_command({}), tmp_path)

    assert written == {"host": {"name": "keep-me"}}
    assert _read_marker(tmp_path) is None


def test_render_host_config_write_command_preserves_user_created_entries(
    tmp_path: Path,
) -> None:
    """
    The server owns the names it injects; user-created config under OTHER names
    always survives. A provider the user adds themselves is never in the marker,
    so cleanup leaves it untouched while removing the injected entries by name.
    """
    injected = {
        "providers": {"gateway": {"kind": "gateway", "default": ["pi"]}},
        "server": "https://injected.example.com",
    }
    _materialize(render_host_config_write_command(injected), tmp_path)
    config_path = tmp_path / ".omnigent" / "config.yaml"
    with open(config_path) as f:
        merged = yaml.safe_load(f)
    merged["providers"]["mine"] = {"kind": "key"}  # user-created, never injected
    merged["default_agent"] = "/user/agent.yaml"  # user-owned top-level key
    config_path.write_text(yaml.safe_dump(merged))

    written = _materialize(render_host_config_write_command({}), tmp_path)

    assert written["providers"] == {"mine": {"kind": "key"}}  # user entry survives
    assert "gateway" not in written["providers"]  # injected name removed
    assert "server" not in written  # injected top-level key removed
    assert written["default_agent"] == "/user/agent.yaml"  # user key untouched
    assert _read_marker(tmp_path) is None


def test_render_host_config_write_command_rename_after_user_edit_leaves_no_stale_default(
    tmp_path: Path,
) -> None:
    """
    Renaming a gateway must not strand the old entry even when the user edited
    it in place — two providers claiming the same ``default`` scope is a sandbox
    load error. Removal is by name, so the old injected name goes regardless.
    """
    _materialize(
        render_host_config_write_command(
            {"providers": {"gateway_a": {"kind": "gateway", "default": ["pi"]}}}
        ),
        tmp_path,
    )
    config_path = tmp_path / ".omnigent" / "config.yaml"
    with open(config_path) as f:
        edited = yaml.safe_load(f)
    edited["providers"]["gateway_a"]["base_url"] = "http://user-edited"  # user edit
    config_path.write_text(yaml.safe_dump(edited))

    written = _materialize(
        render_host_config_write_command(
            {"providers": {"gateway_b": {"kind": "gateway", "default": ["pi"]}}}
        ),
        tmp_path,
    )

    assert sorted(written["providers"]) == ["gateway_b"]
    defaults = [n for n, v in written["providers"].items() if v.get("default") == ["pi"]]
    assert defaults == ["gateway_b"]  # exactly one default, no collision


@pytest.mark.parametrize(
    ("failure_mode", "target_name"),
    [("config", "config.yaml"), ("marker", ".injected_host_config.json")],
)
def test_render_host_config_write_command_interrupted_write_keeps_complete_file(
    tmp_path: Path,
    failure_mode: str,
    target_name: str,
) -> None:
    """A partial temp-file write never truncates either destination file."""
    first = {"providers": {"gateway_a": {"kind": "gateway"}}}
    second = {"providers": {"gateway_b": {"kind": "gateway"}}}
    _materialize(render_host_config_write_command(first), tmp_path)
    config_dir = tmp_path / ".omnigent"
    target = config_dir / target_name
    complete_contents = target.read_bytes()

    hook_dir = tmp_path / "python-hooks"
    hook_dir.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        """\
import json
import os
import yaml

failure = os.environ.get("FAIL_ATOMIC_WRITE")
if failure == "config":
    def fail_yaml(_data, stream, **_kwargs):
        stream.write("partial")
        raise OSError("simulated interrupted config write")
    yaml.safe_dump = fail_yaml
elif failure == "marker":
    def fail_json(_data, stream, *_args, **_kwargs):
        stream.write("{")
        raise OSError("simulated interrupted marker write")
    json.dump = fail_json
"""
    )

    result = _run_write_command(
        render_host_config_write_command(second),
        tmp_path,
        extra_env={
            "FAIL_ATOMIC_WRITE": failure_mode,
            "PYTHONPATH": str(hook_dir),
        },
        check=False,
    )

    assert result.returncode != 0
    assert target.read_bytes() == complete_contents
    assert set(config_dir.iterdir()) == {
        config_dir / "config.yaml",
        config_dir / ".injected_host_config.json",
    }


def test_render_host_config_write_command_empty_payload_without_marker_is_noop(
    tmp_path: Path,
) -> None:
    """The cleanup run on a sandbox that never saw an injection touches nothing."""
    _run_write_command(render_host_config_write_command({}), tmp_path)

    assert not (tmp_path / ".omnigent" / "config.yaml").exists()
    assert _read_marker(tmp_path) is None


def test_render_host_config_write_command_corrupt_marker_degrades_to_additive(
    tmp_path: Path,
) -> None:
    """
    Never delete without evidence: an unreadable marker skips the removal
    (today's additive behavior) rather than guessing what the server owns,
    and the run repairs the marker for the next cycle.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"providers": {"gateway_a": {"kind": "gateway"}}})
    )
    (tmp_path / ".omnigent" / ".injected_host_config.json").write_text("{not json")

    written = _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)

    providers = written["providers"]
    assert providers["gateway_a"] == {"kind": "gateway"}  # not removed
    assert "litellm" in providers
    assert _read_marker(tmp_path) == _GATEWAY_HOST_CONFIG


def test_start_host_without_host_config_runs_cleanup_on_resumable_launcher() -> None:
    """
    A resumable sandbox keeps its filesystem across wakes, so the cleanup
    must run even with no host_config — otherwise entries injected by a
    since-removed block outlive it forever.
    """

    class _ResumableLauncher(_RecordingLauncher):
        can_resume: ClassVar[bool] = True

    launcher = _ResumableLauncher()

    launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )

    cleanup_index = launcher.commands.index(render_host_config_write_command({}))
    host_index = next(
        i for i, cmd in enumerate(launcher.commands) if "omnigent host --server" in cmd
    )
    assert cleanup_index < host_index
