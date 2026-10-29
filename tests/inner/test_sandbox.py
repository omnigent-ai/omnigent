"""Tests for the inner sandbox launcher."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import re
import shlex
import subprocess
import sys
from unittest import mock

import pytest

from omnigent.inner.sandbox import (
    HOST_SANDBOX_BACKEND_ENV,
    SandboxPolicy,
    _launcher_inline_source,
    create_exec_launcher,
    run_launcher,
)
from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR


def _noop_policy() -> SandboxPolicy:
    """A ``backend_type="none"`` / ``active=False`` policy.

    Skips kernel-level activation so tests don't need bwrap.

    :returns: An inactive :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _noop_policy_arg() -> str:
    """The no-op policy in ``run_launcher``'s wire format
    (base64-encoded JSON, matching ``_encode_json_arg``).
    """
    raw = json.dumps(_noop_policy().to_jsonable(), separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii")


@pytest.mark.parametrize("grant_field", ["write_roots", "write_files"])
@pytest.mark.parametrize("grant_parent", [False, True])
def test_exec_launcher_rejects_writable_launcher_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, grant_field: str, grant_parent: bool
) -> None:
    launcher_dir = tmp_path / "launchers"
    launcher_dir.mkdir()
    monkeypatch.setattr("omnigent.inner.sandbox.tempfile.tempdir", str(launcher_dir))
    policy = dataclasses.replace(_noop_policy(), active=True, backend_type="linux_bwrap")
    setattr(policy, grant_field, [tmp_path if grant_parent else launcher_dir])

    with pytest.raises(ValueError, match="Grant a dedicated scratch directory"):
        create_exec_launcher(sys.executable, policy)
    assert list(launcher_dir.iterdir()) == []


def test_exec_launcher_accepts_dedicated_scratch_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher_dir = tmp_path / "launchers"
    launcher_dir.mkdir()
    scratch = launcher_dir / "scratch"
    scratch.mkdir()
    monkeypatch.setattr("omnigent.inner.sandbox.tempfile.tempdir", str(launcher_dir))
    policy = dataclasses.replace(
        _noop_policy(), active=True, backend_type="linux_bwrap", write_roots=[scratch]
    )

    path = pathlib.Path(create_exec_launcher(sys.executable, policy, cwd=str(scratch)))
    try:
        assert path.parent == launcher_dir
        assert path.stat().st_mode & 0o777 == 0o700
    finally:
        path.unlink()


def test_exec_launcher_freezes_phoenix_proc_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filtered child launcher does not need the Phoenix marker."""
    captured: list[SandboxPolicy] = []
    policy = dataclasses.replace(_noop_policy(), active=True, backend_type="linux_bwrap")
    monkeypatch.setenv(HOST_SANDBOX_BACKEND_ENV, "phoenix-kubernetes")
    monkeypatch.setattr(
        "omnigent.inner.sandbox._launcher_inline_source",
        lambda _target, resolved, *, cwd=None: captured.append(resolved) or "pass",
    )

    path = pathlib.Path(create_exec_launcher(sys.executable, policy))
    path.unlink()

    assert captured[0].bind_host_proc is True


def test_run_launcher_emits_logger_checkpoints(caplog) -> None:
    """
    ``run_launcher`` logs three INFO records around the
    sandbox-activation and target-spawn steps. Dropping any of them
    erases the diagnostic signal the claude-sdk leg relies on to
    pinpoint a silent connect hang.
    """
    with caplog.at_level(logging.INFO, logger="omnigent.inner.sandbox"):
        rc = run_launcher(_noop_policy_arg(), sys.executable, ["-c", "pass"])
    assert rc == 0

    messages = [record.getMessage() for record in caplog.records]
    # Absent: wrapper never entered the body.
    assert any("[omnigent-sandbox] activating backend=none active=False" in m for m in messages), (
        messages
    )
    # Absent on an active policy: activation hung.
    assert any("[omnigent-sandbox] activated; spawning target=" in m for m in messages), messages
    # Absent: spawned target hung (the claude-sdk symptom).
    assert any("[omnigent-sandbox] target exited rc=0" in m for m in messages), messages


def test_run_launcher_strips_runner_binding_token_from_target_env(monkeypatch) -> None:
    """The runner tunnel binding token never reaches the spawned target.

    The launcher inherited the runner's full environment and
    ``subprocess.run`` passed it straight to the sandboxed target, so
    agent code could read the runner's control-plane auth secret and
    impersonate the runner. The probe target exits 0 only when the
    token is ABSENT from its inherited ``os.environ``; a non-zero rc
    means the leak is back.

    Spawns a real subprocess (no mocking of ``subprocess.run``) so the
    assertion covers the actual env inheritance the vulnerability used.

    :param monkeypatch: Pytest monkeypatch fixture used to seed the
        binding token into the launcher process's environment.
    """
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "bug-binding-token-secret")
    probe = (
        "import os, sys; "
        f"sys.exit(3 if {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR!r} in os.environ else 0)"
    )

    rc = run_launcher(_noop_policy_arg(), sys.executable, ["-c", probe])

    assert rc == 0, "binding token leaked into the sandboxed target environment"
    # The launcher also drops it from its own process env so any later
    # inheritor (bwrap re-exec) is covered too.
    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in os.environ


def test_run_launcher_propagates_target_returncode(caplog) -> None:
    """``run_launcher`` returns the spawned target's exit code verbatim."""
    with caplog.at_level(logging.INFO, logger="omnigent.inner.sandbox"):
        rc = run_launcher(
            _noop_policy_arg(),
            sys.executable,
            ["-c", "raise SystemExit(7)"],
        )
    assert rc == 7
    messages = [record.getMessage() for record in caplog.records]
    assert any("[omnigent-sandbox] target exited rc=7" in m for m in messages), messages


@pytest.mark.parametrize("without_site_packages", [False, True])
def test_exec_launcher_wrapper_subprocess_emits_markers_to_stderr(
    without_site_packages: bool,
) -> None:
    """
    The generated wrapper script must configure logging so
    ``run_launcher``'s INFO records reach stderr when invoked as a
    subprocess. ``caplog`` alone can't catch a regression in the
    ``basicConfig`` line of the generated script.
    """
    wrapper_path = create_exec_launcher(sys.executable, _noop_policy())
    try:
        launcher = (
            [
                sys.executable,
                "-S",
                "-P",
                "-c",
                _launcher_inline_source(sys.executable, _noop_policy()),
            ]
            if without_site_packages
            else [wrapper_path]
        )
        result = subprocess.run(
            [*launcher, "-c", "pass"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    assert "[omnigent-sandbox] activating backend=none active=False" in result.stderr, (
        result.stderr
    )
    assert "[omnigent-sandbox] activated; spawning target=" in result.stderr, result.stderr
    assert "[omnigent-sandbox] target exited rc=0" in result.stderr, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher script")
def test_exec_launcher_ignores_workspace_modules(tmp_path: pathlib.Path) -> None:
    """Modules in the spawner's cwd never replace the launcher's own imports."""
    for name in ("logging", "json", "base64"):
        (tmp_path / f"{name}.py").write_text(f"raise ImportError('{name} loaded from the cwd')\n")
    wrapper_path = create_exec_launcher(sys.executable, _noop_policy())
    try:
        result = subprocess.run(
            [wrapper_path, "-c", "print('target ran')"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "target ran"


def test_run_launcher_wraps_target_with_strace_when_env_set(monkeypatch, caplog) -> None:
    """
    ``OMNIGENT_SANDBOX_STRACE=1`` must prepend ``strace -f -y -e
    trace=file`` to the spawned target's argv so the wrapper's stderr
    captures file-syscall denials (EACCES) from the sandbox.
    """
    from omnigent.inner import sandbox as sb

    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        sb.shutil,
        "which",
        lambda name: "/usr/bin/strace" if name == "strace" else None,
    )
    monkeypatch.setenv("OMNIGENT_SANDBOX_STRACE", "1")

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.sandbox"):
        rc = sb.run_launcher(_noop_policy_arg(), "/bin/echo", ["hi"])

    assert rc == 0
    assert captured == [
        ["/usr/bin/strace", "-f", "-y", "-e", "trace=file", "--", "/bin/echo", "hi"]
    ], captured
    # WARNING emitted so CI logs unambiguously confirm strace was active.
    assert any("strace active" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


def test_run_launcher_skips_strace_when_binary_missing(monkeypatch, caplog) -> None:
    """If ``OMNIGENT_SANDBOX_STRACE`` is set but strace is not on
    PATH, the wrapper must log a warning and run the target unwrapped
    rather than failing the spawn.
    """
    from omnigent.inner import sandbox as sb

    captured: list[list[str]] = []

    class _FakeCompleted:
        returncode = 0

    def _fake_run(argv, **kwargs):
        captured.append(list(argv))
        return _FakeCompleted()

    monkeypatch.setattr(sb.subprocess, "run", _fake_run)
    monkeypatch.setattr(sb.shutil, "which", lambda name: None)
    monkeypatch.setenv("OMNIGENT_SANDBOX_STRACE", "1")

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.sandbox"):
        rc = sb.run_launcher(_noop_policy_arg(), "/bin/echo", ["hi"])

    assert rc == 0
    # Target ran unwrapped (no strace prefix), proving the missing-binary
    # path falls back gracefully instead of erroring or hanging.
    assert captured == [["/bin/echo", "hi"]], captured
    assert any("strace is not on PATH" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


# ---------------------------------------------------------------------------
# spawn_env_allowlist tests
# ---------------------------------------------------------------------------


def test_sandbox_policy_round_trips_spawn_env_allowlist() -> None:
    """``spawn_env_allowlist`` survives the launcher wire encoding.

    The policy crosses the parent/launcher boundary as JSON baked into
    the generated wrapper script; a field missing from ``to_jsonable``
    or ``from_jsonable`` silently decodes as ``None`` and the launcher
    prune becomes a no-op — the sandbox would quietly lose its env
    containment layer.
    """
    policy = _noop_policy()
    policy.spawn_env_allowlist = ["HOME", "PATH", "PI_CODING_AGENT_DIR"]
    policy.bind_host_proc = True

    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert decoded.spawn_env_allowlist == ["HOME", "PATH", "PI_CODING_AGENT_DIR"]
    assert decoded.bind_host_proc is True
    assert SandboxPolicy.from_jsonable({}).bind_host_proc is None
    assert SandboxPolicy.from_jsonable({"bind_host_proc": "false"}).bind_host_proc is None
    assert SandboxPolicy.from_jsonable({"bind_host_proc": False}).bind_host_proc is False
    # Old payloads (no key) and unset policies must decode to None —
    # "no prune", not "prune everything".
    assert SandboxPolicy.from_jsonable(_noop_policy().to_jsonable()).spawn_env_allowlist is None


def test_credential_source_paths_survive_serialization_and_cloning(tmp_path: pathlib.Path) -> None:
    from omnigent.inner.sandbox import with_additional_read_roots, with_additional_write_roots

    policy = _noop_policy()
    protected = (tmp_path / "token.sock").resolve()
    policy.credential_source_paths = [protected]
    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())
    cloned = with_additional_read_roots(decoded, [tmp_path / "tools"])
    cloned = with_additional_write_roots(cloned, [tmp_path / "scratch"])
    assert cloned.credential_source_paths == [protected]
    assert cloned.credential_source_paths is not decoded.credential_source_paths
    assert SandboxPolicy.from_jsonable({}).credential_source_paths is None


def test_sandbox_policy_round_trips_deny_unix_socket_paths() -> None:
    """``deny_unix_socket_paths`` survives the launcher wire encoding.

    The seatbelt backend reads this list from the *decoded* policy to
    emit its ``(deny network-outbound (remote unix-socket ...))`` rules.
    If the field dropped out of ``to_jsonable`` / ``from_jsonable`` it
    would decode as ``None``, the deny rule would never be emitted, and
    the sandboxed pane could reach the unsandboxed tmux control socket.
    """
    from pathlib import Path

    policy = _noop_policy()
    policy.deny_unix_socket_paths = [Path("/tmp/inst/tmux.sock")]

    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert decoded.deny_unix_socket_paths == [Path("/tmp/inst/tmux.sock")]
    # Old payloads (no key) and unset policies decode to None — "no
    # deny", not an empty-but-present list.
    assert SandboxPolicy.from_jsonable(_noop_policy().to_jsonable()).deny_unix_socket_paths is None


def test_sandbox_policy_round_trips_mask_and_recursive_fields() -> None:
    """``cwd_hidden_scan_recursive`` + ``mask_paths`` survive the wire.

    Both fields cross the parent/helper boundary as JSON. If either
    dropped out of ``to_jsonable`` / ``from_jsonable`` the helper would
    decode the recursive flag as ``False`` and the explicit masks as
    ``None`` — silently weakening the sandbox view.
    """
    from pathlib import Path

    policy = _noop_policy()
    policy.cwd_hidden_scan_recursive = True
    policy.mask_paths = [Path("/work/config/production.key")]

    decoded = SandboxPolicy.from_jsonable(policy.to_jsonable())

    assert decoded.cwd_hidden_scan_recursive is True
    assert decoded.mask_paths == [Path("/work/config/production.key")]
    # Old payloads (no key) and unset policies must decode to the safe
    # defaults: non-recursive and no explicit masks.
    baseline = SandboxPolicy.from_jsonable(_noop_policy().to_jsonable())
    assert baseline.cwd_hidden_scan_recursive is False
    assert baseline.mask_paths is None


def test_with_additional_write_roots_records_mask_scan_skip_roots() -> None:
    """A framework write root added post-resolve is excluded from the
    dotfile mask scan.

    The per-helper scratch tmpdir (holding the egress ``.egress.sock``,
    CA bundle, credential-proxy files) is folded into ``write_roots`` via
    :func:`with_additional_write_roots`. It must also land in
    ``mask_scan_skip_roots`` so the scan never masks ``.egress.sock`` and
    breaks the egress relay. The source policy stays untouched (builders
    are chained off a shared base).
    """
    from pathlib import Path

    from omnigent.inner.sandbox import with_additional_write_roots

    policy = _noop_policy()
    scratch = Path("/tmp/omnigent-helper-ab12")

    augmented = with_additional_write_roots(policy, [scratch])

    resolved = scratch.resolve(strict=False)
    assert resolved in augmented.write_roots
    assert augmented.mask_scan_skip_roots == [resolved]
    # Source policy not mutated.
    assert policy.mask_scan_skip_roots is None
    assert augmented is not policy


def test_with_denied_unix_sockets_resolves_dedupes_and_is_pure() -> None:
    """``with_denied_unix_sockets`` resolves + de-duplicates the socket
    paths and never mutates the input policy.

    The terminal calls this once per instance with the single tmux
    socket, but the helper must be safe to chain (it sits next to the
    other ``with_*`` policy builders) — so we assert it returns a fresh
    policy and leaves the source untouched, and that a repeated path
    collapses to one entry (a duplicate /dev/null mask is harmless but
    a duplicate seatbelt deny line is noise).
    """
    from pathlib import Path

    from omnigent.inner.sandbox import with_denied_unix_sockets

    policy = _noop_policy()
    sock = Path("/tmp/inst/tmux.sock")

    augmented = with_denied_unix_sockets(policy, [sock, sock])

    assert augmented.deny_unix_socket_paths == [sock.resolve(strict=False)]
    # Source policy is not mutated — builders are chained off a shared
    # base policy.
    assert policy.deny_unix_socket_paths is None
    assert augmented is not policy


def test_with_spawn_env_allowlist_sets_sorted_deduped_copy() -> None:
    """``with_spawn_env_allowlist`` normalizes names and never mutates
    the input policy; ``None`` means "didn't opt in" and returns the
    policy unchanged rather than attaching an empty allowlist (which
    would prune EVERYTHING in the launcher).
    """
    from omnigent.inner.sandbox import with_spawn_env_allowlist

    policy = _noop_policy()

    untouched = with_spawn_env_allowlist(policy, None)
    # Same object back: None must not be coerced into a prune-all list.
    assert untouched is policy
    assert policy.spawn_env_allowlist is None

    augmented = with_spawn_env_allowlist(policy, ["PATH", "HOME", "PATH"])
    assert augmented.spawn_env_allowlist == ["HOME", "PATH"]
    # The source policy is not mutated — executors reuse it across
    # with_additional_* chains.
    assert policy.spawn_env_allowlist is None


def test_exec_launcher_prunes_inherited_env_to_spawn_allowlist(tmp_path) -> None:
    """The launcher chain drops env vars outside ``spawn_env_allowlist``
    before the target runs (defense in depth).

    Simulates the regression the field guards against: the launcher
    subprocess is spawned with the FULL host environment (plus a seeded
    ``FAKE_HOST_SECRET``). The real generated wrapper script must hand
    the target only the allowlisted names — if the prune in
    ``run_launcher`` is dropped, the secret shows up in the target's
    dumped environment and this test fails.
    """
    policy = _noop_policy()
    policy.spawn_env_allowlist = ["HOME", "PATH", "KEEP_ME"]
    out_file = tmp_path / "child_env.json"

    wrapper_path = create_exec_launcher(sys.executable, policy)
    dump_code = "import json,os,sys; open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))"
    try:
        result = subprocess.run(
            [wrapper_path, "-c", dump_code, str(out_file)],
            capture_output=True,
            text=True,
            timeout=30,
            # The full-inheritance regression: everything the test
            # process has, plus a recognizable secret.
            env={**os.environ, "FAKE_HOST_SECRET": "PWNED", "KEEP_ME": "yes"},
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    child_env = json.loads(out_file.read_text())
    # The seeded secret was pruned — the launcher enforced containment
    # even though its own environment carried the full host env.
    assert "FAKE_HOST_SECRET" not in child_env, sorted(child_env)
    # Allowlisted names still pass with their values intact (an empty
    # child env would also satisfy the absence check above).
    assert child_env.get("KEEP_ME") == "yes"
    assert child_env.get("PATH") == os.environ["PATH"]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="bubblewrap is Linux-only")
def test_run_launcher_keeps_resolved_proc_bind_after_env_pruning(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proc choice survives launcher environment pruning."""
    from omnigent.inner.bwrap_sandbox import BwrapSandboxBackend
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    monkeypatch.setattr("omnigent.inner.bwrap_sandbox._LAKEBOX_MARKER", tmp_path / "absent")
    monkeypatch.setenv(HOST_SANDBOX_BACKEND_ENV, "phoenix-kubernetes")
    monkeypatch.setattr(
        "omnigent.inner.bwrap_sandbox.shutil.which", lambda _name: "/usr/bin/bwrap"
    )
    policy = BwrapSandboxBackend().resolve(
        OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="linux_bwrap")),
        tmp_path,
    )
    policy = dataclasses.replace(policy, spawn_env_allowlist=["PATH"])
    raw = json.dumps(policy.to_jsonable(), separators=(",", ":")).encode()
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    captured: list[str] = []

    def _capture_exec(_file: str, argv: list[str]) -> None:
        captured.extend(argv)
        raise SystemExit

    monkeypatch.setattr("omnigent.inner.sandbox.create_private_tmpdir", lambda: scratch)
    monkeypatch.setattr(os, "execvp", _capture_exec)

    with mock.patch.dict(os.environ):
        with pytest.raises(SystemExit):
            run_launcher(encoded, sys.executable, ["-c", "pass"], cwd=str(tmp_path))
        assert HOST_SANDBOX_BACKEND_ENV not in os.environ
    assert any(
        captured[index : index + 3] == ["--bind", "/proc", "/proc"]
        for index in range(len(captured) - 2)
    )


def test_exec_launcher_without_allowlist_keeps_inherited_env(tmp_path) -> None:
    """A policy with ``spawn_env_allowlist=None`` (spawner didn't opt
    in) must NOT prune — existing launcher users (claude-sdk, terminal)
    deliberately pass rich environments at spawn time, and a prune-by-
    default would strip them and break those harnesses.
    """
    out_file = tmp_path / "child_env.json"

    wrapper_path = create_exec_launcher(sys.executable, _noop_policy())
    dump_code = "import json,os,sys; open(sys.argv[1],'w').write(json.dumps(dict(os.environ)))"
    try:
        result = subprocess.run(
            [wrapper_path, "-c", dump_code, str(out_file)],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                **os.environ,
                "DELIBERATE_EXTRA": "kept",
                HOST_SANDBOX_BACKEND_ENV: "phoenix-kubernetes",
            },
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result
    child_env = json.loads(out_file.read_text())
    # Opt-in contract: no allowlist, no prune.
    assert child_env.get("DELIBERATE_EXTRA") == "kept"
    assert child_env[HOST_SANDBOX_BACKEND_ENV] == "phoenix-kubernetes"


def _read_launcher(target: str) -> tuple[str, str]:
    """Create a launcher for ``target`` and return ``(path, contents)``."""
    path = create_exec_launcher(target, _noop_policy())
    return path, pathlib.Path(path).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang behaviour")
def test_exec_launcher_shebang_is_bin_sh_not_sys_executable() -> None:
    """The launcher must not name ``sys.executable`` in its shebang.

    Spawners such as the Claude Agent SDK ``execve`` this file directly.
    A ``#!<python>`` line is only executable when that python is itself
    a native binary reachable within the kernel's shebang buffer; when
    it is not, the spawn fails with an opaque ENOEXEC. ``/bin/sh`` is
    the one interpreter that always satisfies the kernel.
    """
    wrapper_path, script = _read_launcher(sys.executable)
    try:
        assert script.startswith("#!/bin/sh\n"), script
        assert f"#!{sys.executable}" not in script, script
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang behaviour")
def test_exec_launcher_runs_when_sys_executable_is_a_shell_script(tmp_path, monkeypatch) -> None:
    """A shim interpreter must still produce a runnable launcher.

    macOS refuses to run a script whose shebang interpreter is itself a
    ``#!`` script, so a python installed behind a wrapper (pyenv-style
    shims, vendored launchers) used to make every wrapped harness
    startup fail with ENOEXEC. Invoking the interpreter as an argument
    of ``/bin/sh`` removes the nesting entirely.
    """
    shim = tmp_path / "python-shim"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(shim))

    wrapper_path, script = _read_launcher(sys.executable)
    try:
        assert script.startswith("#!/bin/sh\n"), script
        result = subprocess.run(
            [wrapper_path, "-c", "pass"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result


@pytest.mark.skipif(os.name == "nt", reason="POSIX shebang behaviour")
def test_exec_launcher_runs_when_interpreter_path_has_a_space(tmp_path, monkeypatch) -> None:
    """A space in the interpreter path must not split the exec.

    A shebang line is split on whitespace, so ``/Applications/My
    Python/bin/python3`` resolved to ``/Applications/My``. The shell
    wrapper quotes the interpreter instead.
    """
    spaced_dir = tmp_path / "My Python"
    spaced_dir.mkdir()
    shim = spaced_dir / "python3"
    shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(shim))

    wrapper_path, _script = _read_launcher(sys.executable)
    try:
        result = subprocess.run(
            [wrapper_path, "-c", "pass"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(wrapper_path)

    assert result.returncode == 0, result


def test_exec_launcher_rejects_an_unnameable_interpreter(monkeypatch) -> None:
    """An empty ``sys.executable`` must fail loudly, not silently.

    Embedded interpreters leave ``sys.executable`` empty; writing the
    launcher anyway yields a file no kernel can run, and the caller
    only learns about it as ENOEXEC from an unrelated spawn.
    """
    monkeypatch.setattr(sys, "executable", "")
    with pytest.raises(OSError, match=re.escape("sys.executable is empty")):
        create_exec_launcher("/bin/true", _noop_policy())
