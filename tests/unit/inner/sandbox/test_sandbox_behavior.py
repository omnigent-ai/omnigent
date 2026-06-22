"""
Cross-backend behavioral parity tests for the spawn-time sandboxes.

These tests assert the observable contract every active sandbox
backend must uphold, run through the live
:func:`omnigent.inner.os_env.create_os_environment` path so they
exercise the helper subprocess end-to-end. They are parametrized
over the active backend on the current host
(:func:`tests.unit.inner.sandbox.conftest.active_sandbox_type`):

- ``linux_bwrap`` when the host is Linux + ``bwrap`` is on ``PATH``,
- ``darwin_seatbelt`` when the host is macOS + ``sandbox-exec`` is
  on ``PATH``.

A regression that violates the contract on either backend fails the
*same* test on the platform that runs the broken backend. The tests
do not branch on ``sys.platform`` — the fixture handles skip/run.

Tests intentionally check **observable** properties (exit codes,
stdout/stderr text, host filesystem state) and never reach into
backend internals (``--bind`` argv tokens, SBPL profile shape).
Backend-specific emit assertions live in the per-backend modules
(``tests/inner/test_bwrap_sandbox.py``,
``tests/inner/test_seatbelt_sandbox.py``).
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import create_os_environment
from tests.unit.inner.sandbox.conftest import run_async

# ---------------------------------------------------------------------------
# Filesystem isolation
# ---------------------------------------------------------------------------


def test_sandbox_blocks_shell_write_outside_cwd(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    A write to a parent-created path outside cwd / scratch fails.

    The strong shape of this test: create a sibling directory in
    the parent shell so we know it IS writable on the host (the
    parent puts a marker file there to prove it), then confirm the
    sandboxed helper cannot write to it. The assertion covers both
    the helper's exit code and the host filesystem (no new file
    appeared at the target path).

    Failure here means the backend leaked write access to an
    arbitrary host path — either by binding too much (bwrap) or by
    emitting too broad an ``allow file-write*`` rule (seatbelt).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    # Sanity: the parent CAN write here, so a failed write inside
    # the sandbox is unambiguously the sandbox's doing.
    (outside_dir / "marker").write_text("parent-can-write")
    outside_target = outside_dir / "pwned.txt"

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=active_sandbox_spec_factory(),
        )
    )
    try:
        result = run_async(os_env.shell(f"printf nope > {outside_target}"))
    finally:
        os_env.close()
    assert result["exit_code"] != 0, (
        f"Write to {outside_target!r} (a sibling of cwd, parent-writable) "
        "returned exit_code=0; the backend leaked an unexpected write surface. "
        f"stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    assert not outside_target.exists(), (
        f"Host filesystem mutated at {outside_target!r}; the sandbox failed "
        "to block the write end-to-end."
    )


def test_sandbox_provides_writable_scratch_tmpdir(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    ``$TMPDIR`` resolves to a real writable directory inside the
    helper, the agent can write+read a probe file there, and the
    helper exits cleanly.

    The per-helper scratch tmpdir is the one always-writable surface
    under the spawn-time backends; regression here would break every
    "write a temp file" pattern in agent code.
    """
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
        )
    )
    try:
        result = run_async(
            os_env.shell(
                'printf "%s\\n" "$TMPDIR"; printf hi > "$TMPDIR/probe"; cat "$TMPDIR/probe"'
            )
        )
    finally:
        os_env.close()
    assert result["exit_code"] == 0, f"$TMPDIR write/read failed: stderr={result.get('stderr')!r}"
    lines = result["stdout"].splitlines()
    assert len(lines) >= 2
    assert lines[0].startswith("/")
    assert lines[1] == "hi"


def test_sandbox_empty_write_paths_blocks_cwd_writes_but_allows_tmpdir(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    With ``write_paths`` unset (the documented backend default), cwd
    is read-only but the scratch ``$TMPDIR`` is still writable.

    The "you wanted hermetic, here's hermetic" contract — flipping
    cwd to writable would silently undo the user's explicit choice
    of an active sandbox backend.
    """
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
        )
    )
    try:
        blocked = run_async(os_env.shell("printf nope > blocked.txt"))
        allowed = run_async(os_env.shell('printf ok > "$TMPDIR/probe"; cat "$TMPDIR/probe"'))
    finally:
        os_env.close()
    assert blocked["exit_code"] != 0, (
        "Write to cwd succeeded under the default (write_paths=None) — cwd should be RO."
    )
    assert allowed["exit_code"] == 0
    assert allowed["stdout"] == "ok"
    assert not (tmp_path / "blocked.txt").exists()


# ---------------------------------------------------------------------------
# S5: HOME-anchored sensitive subpath denial + read_paths dotfile masking.
#
# These are *behavioral* tests — they spawn a real sandboxed helper and
# attempt the read so a regression where the profile says "deny" but the
# kernel doesn't enforce it (wrong SBPL syntax, wrong path
# canonicalisation, wrong bwrap mount shape) fails here. The
# profile-shape unit tests in ``tests/inner/test_seatbelt_sandbox.py`` /
# ``tests/inner/test_bwrap_sandbox.py`` catch profile-generation bugs;
# these catch kernel-enforcement bugs.
# ---------------------------------------------------------------------------


def test_sandbox_blocks_home_library_when_home_read_granted_without_optin(
    tmp_path: Path,
    active_sandbox_type: str,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    S5 (darwin_seatbelt): granting ``$HOME`` in ``read_paths`` does
    NOT silently expose ``$HOME/Library`` — the kernel denies reads
    under it even though a broader allow covers the subtree.

    Spawns a real sandboxed helper, drops a known secret into
    ``$HOME/Library/Preferences/<file>``, and verifies the helper
    cannot read the secret content. A control read of a non-Library
    path under the same HOME grant succeeds — proves the deny is
    targeted and didn't accidentally lock down everything.

    Skipped on Linux because the macOS-specific ``$HOME/Library``
    layout doesn't exist there; the cross-platform dotfile defence
    is covered by the next two tests.
    """
    if active_sandbox_type != "darwin_seatbelt":
        pytest.skip(
            "$HOME/Library default-deny is macOS-specific; Linux relies "
            "on dotfile masking for credential paths (~/.aws etc.)."
        )

    fake_home = tmp_path / "home"
    (fake_home / "Library" / "Preferences").mkdir(parents=True)
    (fake_home / "Library" / "Preferences" / "com.example.creds.plist").write_text(
        "OMNI_S5_LIBRARY_SECRET=must-never-leak"
    )
    (fake_home / "Library" / "Cookies").mkdir()
    (fake_home / "Library" / "Cookies" / "Cookies.binarycookies").write_text(
        "OMNI_S5_COOKIES_SECRET=must-never-leak"
    )
    (fake_home / "Documents").mkdir()
    (fake_home / "Documents" / "report.txt").write_text(
        "OMNI_S5_DOCUMENTS_OK=visible-control-string"
    )
    monkeypatch.setenv("HOME", str(fake_home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = active_sandbox_spec_factory(extra_read_paths=[str(fake_home)])
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=spec)
    )
    try:
        preferences_read = run_async(
            os_env.shell(
                f"cat {fake_home}/Library/Preferences/com.example.creds.plist 2>&1; echo END=$?"
            )
        )
        cookies_read = run_async(
            os_env.shell(
                f"cat {fake_home}/Library/Cookies/Cookies.binarycookies 2>&1; echo END=$?"
            )
        )
        documents_read = run_async(
            os_env.shell(f"cat {fake_home}/Documents/report.txt 2>&1; echo END=$?")
        )
    finally:
        os_env.close()

    assert "OMNI_S5_LIBRARY_SECRET=must-never-leak" not in (
        preferences_read.get("stdout", "") + preferences_read.get("stderr", "")
    ), (
        "$HOME/Library/Preferences was readable despite the default-deny. "
        f"Helper saw: stdout={preferences_read.get('stdout')!r} "
        f"stderr={preferences_read.get('stderr')!r}"
    )
    assert "OMNI_S5_COOKIES_SECRET=must-never-leak" not in (
        cookies_read.get("stdout", "") + cookies_read.get("stderr", "")
    ), (
        "$HOME/Library/Cookies was readable — the Library deny is too "
        "narrow (should cover all of Library, not specific subdirs)."
    )
    assert "OMNI_S5_DOCUMENTS_OK=visible-control-string" in documents_read.get("stdout", ""), (
        "$HOME/Documents control read failed — the Library deny is too "
        "broad and locked down the rest of HOME as a side effect. "
        f"stdout={documents_read.get('stdout')!r} stderr={documents_read.get('stderr')!r}"
    )


def test_sandbox_allows_home_library_when_explicit_optin(
    tmp_path: Path,
    active_sandbox_type: str,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    S5 (darwin_seatbelt): explicitly naming ``$HOME/Library`` (or a
    subtree under it) in ``read_paths`` opts the operator in — the
    default-deny is suppressed and reads under Library succeed.

    This is the escape hatch for legitimate workloads
    (``~/Library/Logs`` debugging tools, app-specific data analysis,
    etc.). Without this assertion a too-aggressive default could
    refuse explicit grants and break operator-intended behaviour.
    """
    if active_sandbox_type != "darwin_seatbelt":
        pytest.skip("$HOME/Library default-deny is macOS-specific.")

    fake_home = tmp_path / "home"
    (fake_home / "Library" / "Logs").mkdir(parents=True)
    (fake_home / "Library" / "Logs" / "app.log").write_text(
        "OMNI_S5_LOGS_VISIBLE=opted-in-content"
    )
    monkeypatch.setenv("HOME", str(fake_home))

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = active_sandbox_spec_factory(
        extra_read_paths=[str(fake_home), str(fake_home / "Library")]
    )
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=spec)
    )
    try:
        logs_read = run_async(
            os_env.shell(f"cat {fake_home}/Library/Logs/app.log 2>&1; echo END=$?")
        )
    finally:
        os_env.close()

    assert "OMNI_S5_LOGS_VISIBLE=opted-in-content" in logs_read.get("stdout", ""), (
        "Explicit ~/Library opt-in didn't take effect. The helper should "
        "be able to read this file. "
        f"stdout={logs_read.get('stdout')!r} stderr={logs_read.get('stderr')!r}"
    )


def test_sandbox_blocks_credential_dotfiles_under_granted_read_path(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S5 (cross-platform): granting a directory in ``read_paths`` does
    NOT expose dotfile-shaped credentials inside it — the dotfile
    masker walks every ``read_paths`` root, not just ``cwd``.

    Pre-fix behaviour: the dotfile masker was cwd-only, so
    ``read_paths: ["~/"]`` (the common shape for an agent that needs
    project siblings) silently leaked ``~/.aws/credentials`` and
    similar. This test creates a fake "home" tree at a controlled
    tmp_path location, grants it via ``read_paths``, and verifies
    that ``.aws/credentials`` is unreachable while a sibling
    non-dotfile path remains readable.
    """
    fake_tree = tmp_path / "tree"
    (fake_tree / ".aws").mkdir(parents=True)
    (fake_tree / ".aws" / "credentials").write_text(
        "[default]\naws_access_key_id=OMNI_S5_AWS_SECRET_LEAK_CANARY"
    )
    (fake_tree / ".ssh").mkdir()
    (fake_tree / ".ssh" / "id_ed25519").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nOMNI_S5_SSH_SECRET_LEAK_CANARY\n-----END"
    )
    (fake_tree / "code").mkdir()
    (fake_tree / "code" / "app.py").write_text("OMNI_S5_NON_DOTFILE_OK=visible-control")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    spec = active_sandbox_spec_factory(extra_read_paths=[str(fake_tree)])
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=spec)
    )
    try:
        aws_read = run_async(os_env.shell(f"cat {fake_tree}/.aws/credentials 2>&1; echo END=$?"))
        ssh_read = run_async(os_env.shell(f"cat {fake_tree}/.ssh/id_ed25519 2>&1; echo END=$?"))
        code_read = run_async(os_env.shell(f"cat {fake_tree}/code/app.py 2>&1; echo END=$?"))
    finally:
        os_env.close()

    assert "OMNI_S5_AWS_SECRET_LEAK_CANARY" not in (
        aws_read.get("stdout", "") + aws_read.get("stderr", "")
    ), (
        ".aws/credentials under a read_paths grant was readable — the "
        "dotfile masker did not walk the read_paths root. "
        f"stdout={aws_read.get('stdout')!r} stderr={aws_read.get('stderr')!r}"
    )
    assert "OMNI_S5_SSH_SECRET_LEAK_CANARY" not in (
        ssh_read.get("stdout", "") + ssh_read.get("stderr", "")
    ), ".ssh/id_ed25519 under a read_paths grant was readable — same regression as the .aws case."
    assert "OMNI_S5_NON_DOTFILE_OK=visible-control" in code_read.get("stdout", ""), (
        "Non-dotfile under the granted read_paths root was unreadable — "
        "the dotfile masker is too broad and masked code/app.py too. "
        f"stdout={code_read.get('stdout')!r} stderr={code_read.get('stderr')!r}"
    )


def test_sandbox_allows_dotfile_under_read_path_when_allowlisted(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    S5 (cross-platform): the ``cwd_allow_hidden`` opt-in extends to
    ``read_paths`` roots — naming a dotfile basename in
    ``cwd_allow_hidden`` lets the helper read that dotfile under any
    granted root, while OTHER dotfiles in the same tree stay masked.

    This is the documented escape hatch for legitimate dotfile
    workloads (an agent that has to read ``.aws/config`` for a
    region check, for example). Without this assertion a
    too-aggressive masker would refuse the operator's explicit
    opt-in and break workflows.
    """
    fake_tree = tmp_path / "tree"
    (fake_tree / ".aws").mkdir(parents=True)
    (fake_tree / ".aws" / "credentials").write_text(
        "OMNI_S5_AWS_OPTIN_VISIBLE=allowlisted-content"
    )
    (fake_tree / ".ssh").mkdir()  # NOT on the allowlist; must stay masked
    (fake_tree / ".ssh" / "id_ed25519").write_text("OMNI_S5_SSH_STILL_MASKED=must-never-leak")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # ``cwd_allow_hidden`` replaces the default ``[".venv"]`` rather
    # than merging with it (spec-self-containment), so we have to
    # re-include ``.venv`` here — without it the masker hides the
    # repo's ``.venv`` and the helper subprocess can't ``execvp``
    # its own Python interpreter.
    spec = active_sandbox_spec_factory(
        extra_read_paths=[str(fake_tree)],
        cwd_allow_hidden=[".venv", ".aws"],
    )
    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(workspace), sandbox=spec)
    )
    try:
        aws_read = run_async(os_env.shell(f"cat {fake_tree}/.aws/credentials 2>&1; echo END=$?"))
        ssh_read = run_async(os_env.shell(f"cat {fake_tree}/.ssh/id_ed25519 2>&1; echo END=$?"))
    finally:
        os_env.close()

    assert "OMNI_S5_AWS_OPTIN_VISIBLE=allowlisted-content" in aws_read.get("stdout", ""), (
        ".aws was in cwd_allow_hidden but the helper couldn't read "
        ".aws/credentials. The allowlist is not being applied to "
        "read_paths roots — operator opt-in is broken. "
        f"stdout={aws_read.get('stdout')!r} stderr={aws_read.get('stderr')!r}"
    )
    assert "OMNI_S5_SSH_STILL_MASKED=must-never-leak" not in (
        ssh_read.get("stdout", "") + ssh_read.get("stderr", "")
    ), (
        ".ssh was NOT on the allowlist but the helper could read it — "
        "the allowlist filter is silently allowing everything."
    )


def test_sandbox_hides_user_dotfiles_in_cwd(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    The backend hides hidden files / dirs in cwd unless they're on
    the allowlist:

    - ``.env`` (file): no read of the secret content.
    - ``.aws`` (dir): credentials inside are not retrievable.
    - ``.venv`` (default allowlist entry): real content shows through.

    The two backends mask via different mechanisms (bwrap binds
    ``/dev/null`` over the path so the file looks empty; Seatbelt
    rejects reads with EPERM), but neither permits the secret string
    to surface. The assertion is on *content absence*, not on a
    specific error mode.
    """
    (tmp_path / ".env").write_text("OMNI_TEST_SECRET=super-secret-value-12345")
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text(
        "[default]\naws_access_key_id=AKIA-PROBE-FAIL-IF-LEAKED"
    )
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "marker").write_text("venv-allowed")

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
        )
    )
    try:
        env_read = run_async(os_env.shell("cat .env 2>&1; echo END=$?"))
        aws_read = run_async(os_env.shell("cat .aws/credentials 2>&1; echo END=$?"))
        venv_read = run_async(os_env.shell("cat .venv/marker"))
    finally:
        os_env.close()

    assert "super-secret-value-12345" not in env_read.get("stdout", "")
    assert "super-secret-value-12345" not in env_read.get("stderr", "")
    assert "AKIA-PROBE-FAIL-IF-LEAKED" not in aws_read.get("stdout", "")
    assert "AKIA-PROBE-FAIL-IF-LEAKED" not in aws_read.get("stderr", "")
    assert venv_read["exit_code"] == 0
    assert venv_read["stdout"] == "venv-allowed"


# ---------------------------------------------------------------------------
# Network deny
# ---------------------------------------------------------------------------


def test_sandbox_allow_network_false_blocks_outbound_connect(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    With ``allow_network=false`` the helper cannot open an outbound
    TCP connection.

    The two backends enforce this very differently:

    - ``linux_bwrap`` adds ``--unshare-net`` so the namespace has no
      route to anywhere except loopback (and not even loopback to
      external hosts).
    - ``darwin_seatbelt`` emits ``(deny network*)`` so every
      ``connect(2)`` returns EPERM regardless of routing.

    The shared assertion is "a Python TCP connect to a routable
    public IP fails". We pick ``1.1.1.1:443`` (Cloudflare DNS) —
    it's globally reachable when network is allowed, so a clean
    "connected" return value proves the deny didn't engage.
    """
    probe = "\n".join(
        [
            "import socket, sys",
            "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
            "s.settimeout(3)",
            "ok = True",
            "try:",
            "    s.connect(('1.1.1.1', 443))",
            "    print('CONNECTED')",
            "except (PermissionError, OSError) as e:",
            "    print(f'BLOCKED:{type(e).__name__}')",
            "    ok = False",
            "finally:",
            "    s.close()",
            "sys.exit(0 if not ok else 1)",
        ]
    )
    spec = active_sandbox_spec_factory(allow_network=False)
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell(f"{sys.executable} -c {_shell_quote(probe)}"))
    finally:
        os_env.close()
    assert result["exit_code"] == 0, (
        "Outbound TCP connect succeeded under allow_network=False. The "
        "sandbox should have blocked it. "
        f"stdout={result.get('stdout')!r} stderr={result.get('stderr')!r}"
    )
    assert "BLOCKED:" in result.get("stdout", ""), (
        f"Expected BLOCKED:* sentinel, got stdout={result.get('stdout')!r}"
    )


def _shell_quote(value: str) -> str:
    """
    Quote *value* for safe inclusion in a POSIX shell command line.

    ``shlex.quote`` would do the job but pulls in a stdlib import we
    don't need elsewhere — the cases here are constrained to
    single-quoted strings with embedded newlines, so single-quoting
    plus a re-quote on any embedded quote works fine.

    :param value: Arbitrary string to embed in a shell command.
    :returns: Shell-safe single-quoted form of *value*.
    """
    return "'" + value.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# Env passthrough
# ---------------------------------------------------------------------------


def test_sandbox_helper_does_not_inherit_unallowlisted_env_vars(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Credentials set in the parent's environment are NOT visible to
    the helper subprocess unless the spec opts them in via
    ``env_passthrough``.

    End-to-end version of the ``build_helper_env`` unit tests in
    :mod:`tests.unit.inner.test_os_env`: spawns a real helper through the
    active backend and reads ``env`` from inside, so a regression
    anywhere in the spawn path (parent env build, ``Popen`` env
    handling, backend env filtering) surfaces here.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA-LEAK-IF-FAILS")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-if-fails")
    monkeypatch.setenv("AWS_PROFILE", "should-be-dropped-without-passthrough")

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
        )
    )
    try:
        result = run_async(os_env.shell("env"))
    finally:
        os_env.close()

    out = result.get("stdout", "") + result.get("stderr", "")
    assert "AKIA-LEAK-IF-FAILS" not in out, (
        "AWS_ACCESS_KEY_ID leaked into the helper's environment. "
        "build_helper_env should have stripped it from the spawn env."
    )
    assert "sk-leak-if-fails" not in out
    assert "should-be-dropped-without-passthrough" not in out


def test_sandbox_helper_inherits_explicit_env_passthrough(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Names listed in ``OSEnvSandboxSpec.env_passthrough`` reach the
    helper subprocess; an unrelated unallowlisted variable is still
    stripped at the same time.

    End-to-end counterpart of the unit test in
    :mod:`tests.unit.inner.test_os_env`: confirms the spec → policy →
    spawn-env plumbing actually carries user-declared passthroughs
    through to the helper without widening the allowlist as a side
    effect.
    """
    monkeypatch.setenv("AWS_PROFILE", "explicit-passthrough-value")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-still-blocked")

    spec = active_sandbox_spec_factory(env_passthrough=["AWS_PROFILE"])
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=spec,
        )
    )
    try:
        result = run_async(os_env.shell('printf "%s\\n" "${AWS_PROFILE:-MISSING}"'))
        leak_check = run_async(os_env.shell("env"))
    finally:
        os_env.close()

    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "explicit-passthrough-value", (
        "AWS_PROFILE was listed in env_passthrough but did not reach the helper: "
        f"stdout={result['stdout']!r} stderr={result.get('stderr')!r}"
    )
    assert "sk-still-blocked" not in (leak_check.get("stdout", "") + leak_check.get("stderr", ""))


# ---------------------------------------------------------------------------
# start_in_scratch
# ---------------------------------------------------------------------------


def test_sandbox_start_in_scratch_helper_starts_in_scratch_tmpdir(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    ``start_in_scratch=True`` lands the helper in the per-helper
    scratch tmpdir, so ``./relative`` writes from the shell tool
    naturally land in the writable scratch dir without prefixing
    every path with ``$TMPDIR``.

    The workspace cwd stays bound at its real absolute path on both
    backends so the agent can still read project files; the
    behavior here is the chdir override, not the bind set.
    """
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
            start_in_scratch=True,
        )
    )
    try:
        pwd_result = run_async(os_env.shell("pwd"))
        write_result = run_async(
            os_env.shell("printf scratch-write > ./probe.txt; cat ./probe.txt")
        )
    finally:
        os_env.close()

    assert pwd_result["exit_code"] == 0
    pwd_out = pwd_result["stdout"].strip()
    assert pwd_out.startswith(("/tmp", tempfile.gettempdir())), (
        f"Helper started outside the system tempdir: pwd={pwd_out!r}. "
        "start_in_scratch should chdir into the per-helper scratch dir."
    )
    assert pwd_out != str(tmp_path.resolve(strict=False)), (
        "Helper still started in the workspace cwd; --chdir override did not take effect."
    )
    assert write_result["exit_code"] == 0, (
        f"Relative write inside scratch failed: stderr={write_result.get('stderr')!r}"
    )
    assert write_result["stdout"] == "scratch-write"
    assert not (tmp_path / "probe.txt").exists(), (
        "./probe.txt leaked back into the workspace cwd; the helper's "
        "cwd was not actually relocated to the scratch dir."
    )


def test_sandbox_start_in_scratch_workspace_remains_readable(
    tmp_path: Path,
    active_sandbox_spec_factory: Callable[..., OSEnvSandboxSpec],
    sandbox_pythonpath_env: None,
) -> None:
    """
    Even when the helper starts in scratch, the workspace cwd is
    still bound for reads. Without this, ``start_in_scratch`` would
    trade write ergonomics for read access — agents would lose
    every absolute-path read into the project tree.
    """
    (tmp_path / "README.md").write_text("hello-from-workspace")

    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=active_sandbox_spec_factory(),
            start_in_scratch=True,
        )
    )
    try:
        absolute_read = run_async(os_env.shell(f"cat {tmp_path.resolve(strict=False)}/README.md"))
    finally:
        os_env.close()

    assert absolute_read["exit_code"] == 0, (
        "Workspace file unreachable from scratch-cwd helper: "
        f"stderr={absolute_read.get('stderr')!r}. The launcher must keep "
        "cwd bound for reads even when --chdir points elsewhere."
    )
    assert absolute_read["stdout"] == "hello-from-workspace"
