"""Regression: an explicit write grant nested under a cwd dotdir is
voided by the dotfile mask (linux_bwrap backend).

The user journey: start a codex-native session whose os_env bundle sets
``sandbox: {type: linux_bwrap, write_paths: ["~/.omnigent/codex-native"]}``
with the session workspace at ``$HOME``. The launcher emits the grant as
``--bind-try ~/.omnigent/codex-native`` and later emits the dotfile mask
``--tmpfs ~/.omnigent``. bwrap resolves overlapping mounts
last-mount-wins, so the mask hides the granted subtree: the codex TUI's
private CODEX_HOME vanishes, codex exits instantly ("Error finding codex
home ... but that path does not exist"), and the session fails with
"Codex app-server never started a thread (startup timed out)". The same
ordering voids the claude CLI wrap's auth grants (``~/.claude.json``,
``~/.claude/.credentials.json``), dropping claude-native into first-run
onboarding.

Two layers, so the bug is pinned on any Linux host:

- The ``*_in_argv`` tests need no user namespaces: they replay bwrap's
  documented last-mount-wins semantics over the generated argv and
  assert every explicitly granted path stays effectively visible.
- ``test_read_through_granted_path_under_home_cwd`` drives the real
  sandboxed helper end-to-end (skips on hosts where bwrap cannot create
  a namespace, e.g. seccomp-restricted CI).

``test_read_through_granted_path_with_project_cwd`` is the control: the
identical grant with a project workspace (no ``.omnigent`` mask in the
profile) isolates any failure of the home-cwd tests to the
mask-after-grant ordering rather than the grant itself.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from tests.inner.sandbox.conftest import _repo_root_for_pythonpath, run_async

_BWRAP = shutil.which("bwrap")

linux_bwrap_only = pytest.mark.skipif(
    not sys.platform.startswith("linux") or _BWRAP is None,
    reason="linux_bwrap requires Linux + bwrap on PATH",
)


def _bwrap_functional() -> bool:
    """Whether bwrap can actually create a namespace on this host.

    A seccomp-confined CI runner can have ``bwrap`` on PATH while the
    kernel denies unprivileged user namespaces; the runtime tests skip
    there instead of failing for a reason unrelated to the bug.
    """
    if _BWRAP is None or not sys.platform.startswith("linux"):
        return False
    try:
        proc = subprocess.run(
            [_BWRAP, "--ro-bind", "/", "/", "/bin/true"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _effective_mount(argv: list[str], path: Path) -> str:
    """Replay bwrap's last-mount-wins semantics over *argv* for *path*.

    :param argv: The generated ``bwrap`` argv.
    :param path: Absolute path to evaluate inside the namespace.
    :returns: ``"exposed"`` when the last mount covering *path* binds
        real host content, ``"masked"`` when it is a ``--tmpfs`` or
        ``/dev/null`` overlay, ``"invisible"`` when no mount covers it.
    """
    probe = str(path)
    state = "invisible"
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        if token in ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try"):
            src, dst = argv[i + 1], argv[i + 2]
            if probe == dst or probe.startswith(dst.rstrip("/") + "/"):
                state = "masked" if src == "/dev/null" else "exposed"
            i += 3
        elif token == "--tmpfs":
            dst = argv[i + 1]
            if probe == dst or probe.startswith(dst.rstrip("/") + "/"):
                state = "masked"
            i += 2
        else:
            i += 1
    return state


def _home_with_codex_bridge(base: Path) -> tuple[Path, Path]:
    """A ``$HOME``-shaped workspace with a codex-native bridge in it.

    :param base: Throwaway parent directory (``tmp_path``).
    :returns: ``(home, codex_home)`` — the workspace root and the
        bridge's private CODEX_HOME under ``home/.omnigent``, next to a
        sibling secret the mask must keep hiding.
    """
    home = base / "home"
    codex_home = home / ".omnigent" / "codex-native" / "bridge0" / "codex-home"
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text("model = 'gpt-5-codex'\n")
    (home / ".omnigent" / "chat.db").write_text("secret\n")
    return home, codex_home


def _codex_bridge_spec(home: Path, cwd: Path) -> OSEnvSpec:
    """The reported bundle: linux_bwrap with the bridge dir granted."""
    return OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            # Repo root so the sandboxed helper can import omnigent.*.
            read_paths=[_repo_root_for_pythonpath()],
            write_paths=[str(home / ".omnigent" / "codex-native")],
            allow_network=False,
        ),
    )


@linux_bwrap_only
def test_codex_bridge_grant_survives_dotdir_mask_in_argv(tmp_path: Path) -> None:
    """The granted codex-native bridge must stay visible with cwd=$HOME.

    A launcher that emits ``--tmpfs <home>/.omnigent`` after the grant's
    ``--bind-try <home>/.omnigent/codex-native`` without re-emitting the
    grant voids it (last-mount-wins) and CODEX_HOME vanishes.
    """
    home, codex_home = _home_with_codex_bridge(tmp_path)
    backend = BwrapSandboxBackend()
    spec = _codex_bridge_spec(home, home)
    policy = backend.resolve(spec, home)
    argv = backend.wrap_launcher_argv(["/bin/true"], policy, home)

    grant = str(home / ".omnigent" / "codex-native")
    assert any(
        argv[i] in ("--bind", "--bind-try") and argv[i + 1] == grant for i in range(len(argv) - 2)
    ), f"precondition: the write grant must be emitted at all. argv: {argv}"

    assert _effective_mount(argv, codex_home / "config.toml") == "exposed", (
        "the --tmpfs mask of ~/.omnigent is emitted after the --bind-try of "
        "the granted ~/.omnigent/codex-native subtree and wins "
        f"(last-mount-wins), voiding the explicit write grant. argv: {argv}"
    )

    # Deny-wins must survive any fix: sibling content stays hidden.
    assert _effective_mount(argv, home / ".omnigent" / "chat.db") != "exposed", (
        f"~/.omnigent content outside the grant must stay masked. argv: {argv}"
    )


@linux_bwrap_only
def test_claude_auth_grants_survive_dotfile_masks_in_argv(tmp_path: Path) -> None:
    """Grant shapes of the claude CLI wrap must stay visible with cwd=$HOME.

    Mirrors the grants ``prepare_claude_cli_path`` adds
    (``_claude_internal_write_roots`` / ``_claude_internal_write_files``):
    with the workspace at ``$HOME``, the ``.claude.json`` write-file grant
    must survive its ``/dev/null`` dotfile mask and the ``.claude``
    subtree grants their ``--tmpfs`` mask; a mask that wins leaves the
    CLI without auth and drops it into first-run onboarding.
    """
    home = tmp_path / "home"
    (home / ".claude" / "sessions").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}\n")
    (home / ".claude.json").write_text("{}\n")

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(home),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[_repo_root_for_pythonpath()],
            write_paths=[str(home / ".claude" / "sessions")],
            write_files=[
                str(home / ".claude.json"),
                str(home / ".claude" / ".credentials.json"),
            ],
            allow_network=False,
        ),
    )
    backend = BwrapSandboxBackend()
    policy = backend.resolve(spec, home)
    argv = backend.wrap_launcher_argv(["/bin/true"], policy, home)

    for granted in (
        home / ".claude.json",
        home / ".claude" / ".credentials.json",
        home / ".claude" / "sessions",
    ):
        assert _effective_mount(argv, granted) == "exposed", (
            f"explicitly granted {granted} is hidden by a later dotfile mask "
            f"(last-mount-wins). argv: {argv}"
        )


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_read_through_granted_path_under_home_cwd(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """End-to-end: the granted CODEX_HOME must be readable in the sandbox.

    This is the exact path the codex TUI stats at startup; a mask that
    wins over the grant hides it and the read fails with a missing-path
    error.
    """
    home, codex_home = _home_with_codex_bridge(tmp_path)

    os_env = create_os_environment(_codex_bridge_spec(home, home))
    try:
        result = run_async(os_env.read(str(codex_home / "config.toml")))
        sibling = run_async(os_env.read(str(home / ".omnigent" / "chat.db")))
    finally:
        os_env.close()

    assert "error" not in result, f"granted CODEX_HOME is invisible inside the sandbox: {result}"
    assert "gpt-5-codex" in str(result.get("content", ""))
    assert "error" in sibling or "secret" not in str(sibling.get("content", "")), (
        f"~/.omnigent content outside the grant must stay masked in the namespace: {sibling}"
    )


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_read_through_granted_path_with_project_cwd(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """Control: the identical grant with a project workspace works today.

    If this control ever fails, a failure of the home-cwd test above is
    environmental, not the mask-after-grant ordering bug.
    """
    home, codex_home = _home_with_codex_bridge(tmp_path)
    project = home / "project"
    project.mkdir()

    os_env = create_os_environment(_codex_bridge_spec(home, project))
    try:
        result = run_async(os_env.read(str(codex_home / "config.toml")))
    finally:
        os_env.close()

    assert "error" not in result, (
        f"control read of granted CODEX_HOME with a project cwd failed: {result}"
    )
    assert "gpt-5-codex" in str(result.get("content", ""))


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_write_through_granted_child_of_read_root_under_home_cwd(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """End-to-end: a write grant nested under a read root stays writable.

    Both grants live under a masked cwd dotdir, so both are replayed
    after the mask; the read-only parent must not be mounted on top of
    the writable child, and the parent itself must stay read-only.
    """
    home = tmp_path / "home"
    read_root = home / ".cfg" / "service"
    write_child = read_root / "child"
    write_child.mkdir(parents=True)

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(home),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[_repo_root_for_pythonpath(), str(read_root)],
            write_paths=[str(write_child)],
            allow_network=False,
        ),
    )
    os_env = create_os_environment(spec)
    try:
        granted = run_async(os_env.write(str(write_child / "data.txt"), "written\n"))
        denied = run_async(os_env.write(str(read_root / "nope.txt"), "denied\n"))
    finally:
        os_env.close()

    assert "error" not in granted, (
        f"write into the granted child under an overlapping read root failed: {granted}"
    )
    assert "error" in denied, (
        f"the read root outside the write child must stay read-only: {denied}"
    )


@pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)
def test_granted_file_stays_visible_while_masked_siblings_stay_hidden(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """End-to-end: a granted file inside a re-masked dotdir is readable
    while its ungranted siblings stay hidden inside the namespace.
    """
    home = tmp_path / "home"
    grant = home / ".grant"
    secret_dir = grant / ".secret"
    secret_dir.mkdir(parents=True)
    auth = secret_dir / "auth.json"
    auth.write_text('{"token": "granted"}\n')
    (secret_dir / "other.txt").write_text("private\n")

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(home),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[_repo_root_for_pythonpath()],
            write_paths=[str(grant)],
            write_files=[str(auth)],
            allow_network=False,
        ),
    )
    os_env = create_os_environment(spec)
    try:
        granted = run_async(os_env.read(str(auth)))
        sibling = run_async(os_env.read(str(secret_dir / "other.txt")))
    finally:
        os_env.close()

    assert "error" not in granted, (
        f"the granted file inside the re-masked dotdir is invisible: {granted}"
    )
    assert "granted" in str(granted.get("content", ""))
    assert "error" in sibling or "private" not in str(sibling.get("content", "")), (
        f"an ungranted sibling of the granted file leaked through the mask: {sibling}"
    )
