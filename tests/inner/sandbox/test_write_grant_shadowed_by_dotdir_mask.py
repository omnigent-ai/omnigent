"""Regression: explicit linux_bwrap grants under a masked cwd dotpath.

With the workspace at ``$HOME``, the dotfile mask of ``~/.omnigent`` (or
the ``/dev/null`` overlay of ``~/.claude.json``) is emitted after the
grant binds and must not void codex-native's ``write_paths`` bridge
directory or the claude CLI wrap's auth grants. These tests drive the
real sandboxed helper and skip where bwrap cannot create a namespace;
``tests/inner/test_bwrap_sandbox.py`` covers the generated argv.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from tests.inner.sandbox.conftest import _bwrap_functional, _repo_root_for_pythonpath, run_async

requires_bwrap_namespace = pytest.mark.skipif(
    not _bwrap_functional(),
    reason="bwrap cannot create namespaces on this host",
)


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


@requires_bwrap_namespace
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


@requires_bwrap_namespace
def test_overlapping_read_and_write_grants_keep_write_precedence_under_home_cwd(
    tmp_path: Path,
    sandbox_pythonpath_env: None,
) -> None:
    """End-to-end: overlapping grants under a masked cwd dotdir keep the
    main bind pass's write-over-read precedence after they are replayed.

    A write child under a read root stays writable while the read root
    stays read-only, and a read child under a write root stays writable
    while its ungranted dotfiles stay hidden.
    """
    home = tmp_path / "home"
    read_root = home / ".cfg" / "read-parent"
    write_child = read_root / "child"
    write_child.mkdir(parents=True)
    write_parent = home / ".cfg" / "write-parent"
    read_child = write_parent / "readonly"
    read_child.mkdir(parents=True)
    (read_child / ".env").write_text("SECRET=1\n")

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(home),
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            read_paths=[_repo_root_for_pythonpath(), str(read_root), str(read_child)],
            write_paths=[str(write_child), str(write_parent)],
            allow_network=False,
        ),
    )
    os_env = create_os_environment(spec)
    try:
        child_write = run_async(os_env.write(str(write_child / "data.txt"), "written\n"))
        root_write = run_async(os_env.write(str(read_root / "nope.txt"), "denied\n"))
        nested_write = run_async(os_env.write(str(read_child / "data.txt"), "written\n"))
        dotfile = run_async(os_env.read(str(read_child / ".env")))
    finally:
        os_env.close()

    assert "error" not in child_write, (
        f"write into the granted child under an overlapping read root failed: {child_write}"
    )
    assert (write_child / "data.txt").read_text() == "written\n"
    assert "error" in root_write, (
        f"the read root outside the write child must stay read-only: {root_write}"
    )
    assert not (read_root / "nope.txt").exists(), "write leaked through the read-only root"
    assert "error" not in nested_write, (
        f"write into the read root nested under the write root failed: {nested_write}"
    )
    assert (read_child / "data.txt").read_text() == "written\n"
    assert "error" in dotfile or "SECRET" not in str(dotfile.get("content", "")), (
        f"an ungranted dotfile inside the nested read root leaked: {dotfile}"
    )


@requires_bwrap_namespace
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
