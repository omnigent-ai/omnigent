"""E2E regression test: a codex bundle's skills must be readable from the
bundle's own ``linux_bwrap`` sandbox.

The reported user journey: a ``codex`` harness bundle ships skills and an
``os_env`` sandbox (``type: linux_bwrap``, ``cwd_allow_hidden: [.git]``,
``write_paths: ["."]``). The session starts cleanly and Codex *discovers*
the skills — the model's manifest lists each skill's name, description and
the path to its ``SKILL.md`` — but every tool the bundle is allowed to use
gets ``No such file or directory`` for that very path. Two product
mechanisms compose into the dead end:

* the wrapped codex executor stages the per-conversation ``CODEX_HOME``
  inside the workspace at ``<cwd>/.codex-tmp/omnigent-codex-home-*`` and
  symlinks each skill to its out-of-cwd bundle directory, while
* the bwrap backend tmpfs-masks every hidden cwd entry not in
  ``cwd_allow_hidden`` — ``.codex-tmp`` included — and the namespace never
  mounts the bundle directory, so even an allow-listed ``.codex-tmp``
  leaves the skill symlinks dangling (and exposes the bridged
  ``auth.json``, which is why the allow-list is not a workaround).

These tests stage a REAL codex session (the real ``codex app-server``
handshake, the real skill population) and then hold the REAL bwrap mount
plan for the reporter's sandbox spec against the path the harness just
published. They assert the FIXED contract — the published ``SKILL.md``
must be readable inside the sandbox while the rest of the temp
``CODEX_HOME`` (``auth.json``) stays hidden — so they FAIL on the broken
build and PASS once a fix lands, whatever shape the fix takes (copying
skills and re-exposing the subtree, staging the home outside cwd with a
read grant, ...).

``test_manifest_skill_readable_through_real_bwrap_namespace`` additionally
executes the wrapped argv in a real namespace; it skips where unprivileged
namespace creation is unavailable (e.g. inside a nested CI sandbox), while
the mount-plan tests run everywhere.

Usage::

    pytest tests/e2e/test_codex_bundle_skill_sandbox_readable_e2e.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
from omnigent.inner.codex_executor import _CodexAppServerSession
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from tests.e2e._harness_probes import cli_unavailable_reason

pytestmark = [
    pytest.mark.skipif(
        (_codex_reason := cli_unavailable_reason("codex")) is not None,
        reason=f"requires a runnable 'codex' CLI; {_codex_reason}",
    ),
    pytest.mark.skipif(
        not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
        reason="linux_bwrap sandbox requires Linux with bubblewrap installed",
    ),
]

# Unforgeable body marker: if this string comes back through the sandbox,
# the bundle's own tools genuinely read the staged SKILL.md.
_SKILL_NAME = "implementation-planning"
_SKILL_MARKER = "bundle-skill methodology marker 4f8a1c"


@cache
def _bwrap_namespace_unavailable() -> str | None:
    """Return a skip reason when bwrap cannot create namespaces here.

    Nested sandboxes (CI agents already running inside bubblewrap) cannot
    create user namespaces, so mount-executing tests must skip there while
    the mount-plan tests keep running.

    :returns: ``None`` when a trivial bwrap namespace spawns, else the
        first stderr line explaining why it cannot.
    """
    probe = subprocess.run(
        ["bwrap", "--ro-bind", "/", "/", "true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode == 0:
        return None
    detail = (probe.stderr or probe.stdout).strip().splitlines()
    return detail[0] if detail else f"bwrap exited {probe.returncode}"


def _build_workspace_and_bundle(root: Path) -> tuple[Path, Path]:
    """Create a git-clone-like workspace and a bundle shipping one skill.

    :param root: Per-test temp root.
    :returns: ``(workspace, bundle_dir)`` — the session cwd and the
        materialized agent-bundle root whose ``skills/`` subdir carries
        ``implementation-planning``.
    """
    workspace = root / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo repo\n")
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=e2e@example.com",
            "-c",
            "user.name=e2e",
            "commit",
            "-qm",
            "init",
        ],
        cwd=workspace,
        check=True,
    )

    bundle = root / "bundle"
    skill_dir = bundle / "skills" / _SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {_SKILL_NAME}\n"
        "description: plan the implementation before touching code\n---\n\n"
        f"{_SKILL_MARKER}\n"
    )
    return workspace, bundle


async def _stage_codex_session(workspace: Path, bundle: Path) -> _CodexAppServerSession:
    """Start a real codex app-server session so the product stages skills.

    This is the same start path a runner session takes: the executor
    creates the per-conversation ``CODEX_HOME``, populates its ``skills/``
    from the bundle, and boots ``codex app-server`` through the initialize
    handshake. No model turn (and no provider auth) is needed — staging
    and discovery happen before the first turn.

    :param workspace: The session cwd (the user's clone).
    :param bundle: The materialized bundle root with ``skills/``.
    :returns: The started session; callers must ``await session.close()``.
    """
    session = _CodexAppServerSession(
        codex_path=shutil.which("codex") or "codex",
        cwd=str(workspace),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        },
        tool_executor=None,
        bundle_dir=bundle,
        skills_filter="all",
    )
    await session.start()
    return session


def _wrapped_argv(
    workspace: Path, argv: list[str], *, extra_allow_hidden: list[str] | None = None
) -> list[str]:
    """Build the exact bwrap argv the runner would execute for a helper.

    Uses the reporter's sandbox spec: ``type: linux_bwrap``,
    ``cwd_allow_hidden: [.git]``, ``write_paths: ["."]``.

    :param workspace: The session cwd the sandbox is anchored on.
    :param argv: The helper command to wrap, e.g. ``["/bin/cat", path]``.
    :param extra_allow_hidden: Extra ``cwd_allow_hidden`` basenames on top
        of the reporter's ``[".git"]``.
    :returns: The complete bwrap argv.
    """
    backend = BwrapSandboxBackend()
    spec = OSEnvSpec(
        type="os_env",
        sandbox=OSEnvSandboxSpec(
            type="linux_bwrap",
            cwd_allow_hidden=[".git", *(extra_allow_hidden or [])],
            write_paths=["."],
        ),
    )
    policy = backend.resolve(spec, workspace)
    return backend.wrap_launcher_argv(argv, policy, workspace)


# ---------------------------------------------------------------------------
# Namespace mount-plan emulation
# ---------------------------------------------------------------------------
#
# bwrap applies its mount operations in argv order; for any path the LAST
# operation whose destination is an ancestor-or-equal wins, and bwrap
# creates missing directories for later mount destinations. Symlinks are
# resolved INSIDE the namespace, so a link whose target is not mounted
# dangles. The helpers below emulate exactly that much — enough to decide
# whether the file the harness published is readable — against the argv
# the product itself emitted, so they stay correct for any fix shape.

_MASK_OPS = {"--tmpfs", "--dir", "--proc", "--dev", "--mqueue"}
_BIND_OPS = {"--ro-bind", "--ro-bind-try", "--bind", "--bind-try", "--dev-bind"}


def _mount_ops(argv: list[str]) -> list[tuple[str, str, str]]:
    """Parse a bwrap argv into ordered ``(op, src, dst)`` mount operations.

    :param argv: The full bwrap argv from :func:`_wrapped_argv`.
    :returns: Mount operations in application order; non-mount options are
        ignored.
    """
    ops: list[tuple[str, str, str]] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _BIND_OPS or tok == "--symlink":
            ops.append((tok, argv[i + 1], argv[i + 2]))
            i += 3
        elif tok in _MASK_OPS:
            ops.append((tok, "", argv[i + 1]))
            i += 2
        else:
            i += 1
    return ops


def _covers(dst: str, path_str: str) -> bool:
    """Whether mount destination *dst* is an ancestor-or-equal of *path_str*."""
    return path_str == dst or path_str.startswith(dst.rstrip("/") + "/")


def _winning(ops: list[tuple[str, str, str]], path_str: str) -> tuple[int, str, str, str] | None:
    """The last (= effective) mount operation covering *path_str*, if any."""
    win: tuple[int, str, str, str] | None = None
    for idx, (op, src, dst) in enumerate(ops):
        if _covers(dst, path_str):
            win = (idx, op, src, dst)
    return win


def _mount_at_or_below(
    ops: list[tuple[str, str, str]], path_str: str, after_idx: int = -1
) -> bool:
    """Whether a later mount's destination sits at-or-below *path_str*.

    bwrap creates every directory on the way to a mount destination, so
    such a mount implies *path_str* exists as a directory inside the
    namespace even when an earlier tmpfs emptied it.
    """
    prefix = path_str.rstrip("/") + "/"
    return any(
        dst == path_str or dst.startswith(prefix) for _op, _src, dst in ops[after_idx + 1 :]
    )


def _sandbox_readable(
    path: Path, ops: list[tuple[str, str, str]], depth: int = 0
) -> tuple[bool, str]:
    """Would *path* resolve to a readable regular file inside the namespace?

    Walks the path component-wise the way the in-namespace kernel would:
    every prefix is checked against the winning mount, and symlinks found
    on the winning bind's host side are re-resolved through the namespace
    view (which is what makes an escaping symlink dangle when its target
    is unmounted).

    :param path: Absolute path as the sandboxed process would open it.
    :param ops: Parsed mount plan from :func:`_mount_ops`.
    :param depth: Symlink-hop recursion guard.
    :returns: ``(readable, detail)`` — on success *detail* is the
        effective host path whose bytes the namespace would serve, else a
        human-readable reason.
    """
    if depth > 8:
        return False, f"{path}: symlink chain exceeded 8 hops"
    parts = Path(path).parts
    cur = Path(parts[0])
    host = cur
    i = 1
    while i < len(parts):
        cur = cur / parts[i]
        cur_str = str(cur)
        final = i == len(parts) - 1
        win = _winning(ops, cur_str)
        if win is None:
            if not final and _mount_at_or_below(ops, cur_str):
                i += 1
                continue  # implicit parent directory of a later mount point
            return False, f"{cur_str}: nothing mounts it inside the namespace"
        idx, op, src, dst = win
        if op in _MASK_OPS:
            if not final and _mount_at_or_below(ops, cur_str, idx):
                i += 1
                continue  # a later mount re-creates this directory inside the mask
            return False, f"{cur_str}: hidden by `{op} {dst}`"
        if op == "--symlink":
            rest = parts[i + 1 :]
            return _sandbox_readable(Path(src).joinpath(*rest), ops, depth + 1)
        host = Path(src) / cur.relative_to(dst) if src != dst else cur
        if not os.path.lexists(host):
            return False, f"{cur_str}: absent under bind source {src!r}"
        if host.is_symlink():
            raw = os.readlink(host)
            resolved = (
                Path(raw) if os.path.isabs(raw) else Path(os.path.normpath(host.parent / raw))
            )
            rest = parts[i + 1 :]
            # Identity binds (src == dst) keep host and namespace paths
            # interchangeable, so the target re-enters namespace resolution.
            return _sandbox_readable(resolved.joinpath(*rest), ops, depth + 1)
        i += 1
    if not host.is_file():
        return False, f"{host}: not a regular file on the winning bind"
    return True, str(host)


# ---------------------------------------------------------------------------
# Tests — FIXED contract: fail on the broken build, pass once a fix lands.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_skill_path_readable_in_sandbox_mount_plan(tmp_path: Path) -> None:
    """The SKILL.md path the harness publishes must survive its own sandbox.

    Stages a real codex session (real CLI, real skill population), then
    resolves the reporter's sandbox spec through the real bwrap backend
    and asserts that the very path Codex placed in the skill manifest is
    readable — with the skill body — inside the mount plan the runner
    would execute, while the bridged ``auth.json`` stays hidden.

    Broken build: ``--tmpfs <cwd>/.codex-tmp`` hides the manifest path, so
    the readability assertion fails with the exact composition the bug
    report describes.
    """
    workspace, bundle = _build_workspace_and_bundle(tmp_path)
    session = await _stage_codex_session(workspace, bundle)
    try:
        codex_home = session._codex_home_dir
        assert codex_home is not None
        manifest = codex_home / "skills" / _SKILL_NAME / "SKILL.md"
        # Discovery half of the report: the staged skill exists host-side,
        # which is why Codex lists it in the model's manifest.
        assert manifest.exists(), "codex staging no longer exposes the bundle skill at all"

        # The executor bridges the user's credentials into the temp home on
        # authenticated hosts; seed the same file so the security half of
        # the contract is checked even on credential-less CI.
        auth = codex_home / "auth.json"
        if not auth.exists():
            auth.write_text('{"OPENAI_API_KEY": "sk-e2e-do-not-expose"}')

        ops = _mount_ops(_wrapped_argv(workspace, ["/bin/cat", str(manifest)]))

        readable, detail = _sandbox_readable(manifest, ops)
        assert readable, (
            "bundle skill is discoverable but unreadable from the bundle's own "
            f"sandbox: the manifest path {manifest} is not readable inside the "
            f"bwrap namespace ({detail}); the agent silently loses its methodology"
        )
        assert _SKILL_MARKER in Path(detail).read_text(), (
            f"sandbox serves {detail} for the manifest path, which does not "
            "contain the staged skill body"
        )

        auth_readable, auth_detail = _sandbox_readable(auth, ops)
        assert not auth_readable, (
            "fixing skill readability must not expose the rest of the temp "
            f"CODEX_HOME: auth.json is readable inside the sandbox ({auth_detail})"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    _bwrap_namespace_unavailable() is not None,
    reason=f"cannot execute bwrap namespaces here: {_bwrap_namespace_unavailable()}",
)
async def test_manifest_skill_readable_through_real_bwrap_namespace(tmp_path: Path) -> None:
    """Same contract, executed in a real bwrap namespace.

    Runs ``cat <manifest SKILL.md>`` through the product-wrapped bwrap
    argv — the same journey the bundle's ``sys_os_shell`` takes — and
    asserts the skill body comes back. Broken build: ``cat`` exits
    non-zero with ``No such file or directory``, exactly the reported
    symptom.
    """
    workspace, bundle = _build_workspace_and_bundle(tmp_path)
    session = await _stage_codex_session(workspace, bundle)
    try:
        codex_home = session._codex_home_dir
        assert codex_home is not None
        manifest = codex_home / "skills" / _SKILL_NAME / "SKILL.md"
        assert manifest.exists(), "codex staging no longer exposes the bundle skill at all"

        wrapped = _wrapped_argv(workspace, ["/bin/cat", str(manifest)])
        proc = subprocess.run(wrapped, capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 0, (
            "reading the manifest SKILL.md from inside the bundle's own "
            f"sandbox failed (rc={proc.returncode}): {proc.stderr.strip()!r}"
        )
        assert _SKILL_MARKER in proc.stdout, (
            f"sandboxed read of the manifest SKILL.md returned no skill body: {proc.stdout!r}"
        )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_codex_home_staging_keeps_user_clone_clean(tmp_path: Path) -> None:
    """Session staging must not dirty ``git status`` in the user's clone.

    The report's side-effect facet: staging the per-conversation
    ``CODEX_HOME`` under ``<cwd>/.codex-tmp`` leaves an untracked
    directory in the user's repository for the session's lifetime.
    """
    workspace, bundle = _build_workspace_and_bundle(tmp_path)
    session = await _stage_codex_session(workspace, bundle)
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert status.strip() == "", (
            "starting a codex session dirties the user's clone: "
            f"git status reports {status.strip()!r}"
        )
    finally:
        await session.close()
