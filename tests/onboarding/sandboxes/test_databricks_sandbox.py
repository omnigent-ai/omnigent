"""Tests for :mod:`omnigent.onboarding.sandboxes.databricks_sandbox`."""

from __future__ import annotations

import io
import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import click
import pytest

from omnigent.onboarding.sandboxes import databricks_sandbox as dbx
from omnigent.onboarding.sandboxes.databricks_sandbox import (
    AUTH_HINT,
    INSTALL_HINT,
    DatabricksSandboxLauncher,
)

# ── Fake `databricks` CLI ───────────────────────────────────
#
# The launcher's whole transport is `subprocess.run` / `subprocess.Popen`
# against the real Databricks CLI, which the test environment neither has
# nor should reach. These are hand-rolled recorders (never MagicMock: the
# launcher's argv must hit an explicit stub, not silently succeed), swapped
# in with `monkeypatch.setattr(dbx.subprocess, …)` — the same pattern
# tests/onboarding/sandboxes/test_bootstrap.py uses.


@dataclass
class _Reply:
    """
    One canned CLI response.

    :param returncode: Exit code the fake CLI reports.
    :param stdout: Captured standard output.
    :param stderr: Captured standard error.
    """

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _Call:
    """
    One recorded ``subprocess.run`` invocation.

    :param argv: The argv list the launcher built.
    :param stdin: Whatever was passed as ``stdin``, or ``None``.
    """

    argv: list[str]
    stdin: Any = None


@dataclass
class _FakeCLI:
    """
    Recorder + canned-reply table for the fake Databricks CLI.

    :param replies: Reply queue keyed by subcommand (``"create"``,
        ``"ssh"``, …). Each call pops the next reply for its subcommand,
        falling back to a bare success once the queue empties, so a test
        only has to script the responses it cares about.
    :param calls: Every invocation, in order.
    """

    replies: dict[str, list[_Reply]] = field(default_factory=dict)
    calls: list[_Call] = field(default_factory=list)

    def reply_with(self, subcommand: str, *replies: _Reply) -> None:
        """
        Queue responses for one subcommand.

        :param subcommand: e.g. ``"status"``.
        :param replies: Responses, returned in order.
        """
        self.replies.setdefault(subcommand, []).extend(replies)

    def argvs(self, subcommand: str) -> list[list[str]]:
        """
        Return every recorded argv for one subcommand.

        :param subcommand: e.g. ``"config"``.
        :returns: The matching argv lists, in call order.
        """
        return [call.argv for call in self.calls if _subcommand(call.argv) == subcommand]

    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand in for ``subprocess.run``, recording and replying."""
        self.calls.append(_Call(argv=list(argv), stdin=kwargs.get("stdin")))
        queue = self.replies.get(_subcommand(argv), [])
        reply = queue.pop(0) if queue else _Reply()
        return subprocess.CompletedProcess(
            args=argv,
            returncode=reply.returncode,
            stdout=reply.stdout,
            stderr=reply.stderr,
        )


def _subcommand(argv: list[str]) -> str:
    """
    Extract the ``databricks sandbox <sub>`` subcommand from an argv.

    :param argv: The full argv list.
    :returns: The subcommand, or ``""`` when the argv is not shaped like
        a sandbox call.
    """
    if "sandbox" not in argv:
        return ""
    index = argv.index("sandbox") + 1
    return argv[index] if index < len(argv) else ""


def _install(monkeypatch: pytest.MonkeyPatch, cli: _FakeCLI | None = None) -> _FakeCLI:
    """
    Swap in the fake CLI and make ``shutil.which`` resolve the binary.

    :param monkeypatch: pytest's patcher.
    :param cli: An existing recorder to reuse, or ``None`` for a fresh one.
    :returns: The recorder now backing ``subprocess.run``.
    """
    fake = cli if cli is not None else _FakeCLI()
    monkeypatch.setattr(dbx.subprocess, "run", fake.run)
    monkeypatch.setattr(dbx.shutil, "which", lambda name: f"/fake/{name}")
    return fake


def _remote_command(argv: list[str]) -> str:
    """
    Return the remote command an ``ssh`` argv carries after ``--``.

    :param argv: A recorded ``ssh`` argv.
    :returns: The single remote-command element.
    """
    return argv[argv.index("--") + 1]


# ── capabilities ────────────────────────────────────────────


def test_capabilities_declare_port_forward_and_resume() -> None:
    """
    The two flags that distinguish this provider from every other
    exec-model launcher must be True: `databricks sandbox ssh` is real
    SSH (so `-L` forwarding works) and sandboxes restart with their disk
    intact (so a dormant host resumes under the same id). Both were
    verified live; if either regresses to False the bootstrap silently
    routes around a capability the platform actually has.
    """
    capabilities = DatabricksSandboxLauncher().capabilities
    assert capabilities.local_port_forward is True
    assert capabilities.resume_stopped is True
    assert capabilities.managed_launch is True
    assert capabilities.programmatic_terminate is True
    assert capabilities.file_copy is True


# ── prepare ─────────────────────────────────────────────────


def test_prepare_raises_install_hint_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    No `databricks` on PATH must fail with the install remediation, not
    a bare FileNotFoundError from the first subprocess call.
    """
    monkeypatch.setattr(dbx.shutil, "which", lambda name: None)
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().prepare()
    assert str(exc.value) == INSTALL_HINT


def test_prepare_raises_auth_hint_when_list_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    An installed-but-unauthenticated CLI must point at `databricks auth
    login`, and must surface the CLI's own reason verbatim so an
    operator can tell "wrong workspace" from "key not registered".
    """
    cli = _install(monkeypatch)
    cli.reply_with("list", _Reply(returncode=1, stderr="cannot resolve credentials"))
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().prepare()
    assert AUTH_HINT in str(exc.value)
    assert "cannot resolve credentials" in str(exc.value)


def test_prepare_accepts_authenticated_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful `sandbox list` is the whole preflight."""
    cli = _install(monkeypatch)
    cli.reply_with("list", _Reply(stdout="[]"))
    DatabricksSandboxLauncher().prepare()
    assert cli.argvs("list") == [["databricks", "sandbox", "list", "-o", "json"]]


# ── provision ───────────────────────────────────────────────


def test_provision_creates_and_returns_sandbox_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `create --json` is the id source. The label rides as `--name`, and
    the returned id (not the label) is what every later primitive uses.
    """
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"sandboxId": "spanking-pitta-1649"})))
    sandbox_id = DatabricksSandboxLauncher().provision("managed-a1b2c3d4")
    assert sandbox_id == "spanking-pitta-1649"
    assert cli.argvs("create") == [
        ["databricks", "sandbox", "create", "--name", "managed-a1b2c3d4", "--json"]
    ]


def test_provision_disables_autostop_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A managed host sits idle between turns, so provision must follow
    `create` with `config --no-autostop`. Without it the sandbox reaps
    itself on the workspace's default idle timeout and the session dies.
    """
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"sandboxId": "sb-1"})))
    DatabricksSandboxLauncher().provision("managed-a1b2c3d4")
    assert cli.argvs("config") == [["databricks", "sandbox", "config", "sb-1", "--no-autostop"]]


def test_provision_applies_idle_timeout_when_autostop_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With `no_autostop=False`, a configured idle timeout is applied
    instead — the two knobs are mutually exclusive on the CLI.
    """
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"sandboxId": "sb-1"})))
    DatabricksSandboxLauncher(no_autostop=False, idle_timeout="4h").provision("label")
    assert cli.argvs("config") == [
        ["databricks", "sandbox", "config", "sb-1", "--idle-timeout", "4h"]
    ]


def test_provision_skips_config_when_no_policy_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No policy configured → no `config` call at all."""
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"sandboxId": "sb-1"})))
    DatabricksSandboxLauncher(no_autostop=False).provision("label")
    assert cli.argvs("config") == []


def test_provision_raises_when_create_returns_no_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A create that succeeds but reports no `sandboxId` must fail loud —
    returning an empty id would strand every later primitive on a
    sandbox that does not exist.
    """
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"status": "Running"})))
    with pytest.raises(click.ClickException, match="no 'sandboxId'"):
        DatabricksSandboxLauncher().provision("label")


def test_provision_raises_on_unparseable_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-JSON stdout must surface as a clear error, not a ValueError."""
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout="Creating sandbox…"))
    with pytest.raises(click.ClickException, match="unparseable JSON"):
        DatabricksSandboxLauncher().provision("label")


def test_profile_is_threaded_through_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A configured profile must reach every CLI invocation. A server
    running under a service-principal profile that leaked a single
    unprofiled call would silently fall back to the DEFAULT profile —
    i.e. a different workspace.
    """
    cli = _install(monkeypatch)
    cli.reply_with("create", _Reply(stdout=json.dumps({"sandboxId": "sb-1"})))
    launcher = DatabricksSandboxLauncher(profile="sandbox-sp")
    launcher.provision("label")
    launcher.run("sb-1", "true")
    launcher.terminate("sb-1")
    for call in cli.calls:
        assert "-p" in call.argv, call.argv
        assert call.argv[call.argv.index("-p") + 1] == "sandbox-sp"


def test_profile_flag_never_crosses_the_ssh_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `-p` is a CLI flag, not an ssh flag: it must appear BEFORE `--`.
    Past the separator the Databricks CLI hands arguments straight to
    ssh, where `-p` means "port" and would silently redirect the
    connection.
    """
    cli = _install(monkeypatch)
    DatabricksSandboxLauncher(profile="sandbox-sp").run("sb-1", "echo hi")
    argv = cli.argvs("ssh")[0]
    assert argv.index("-p") < argv.index("--")


def test_cli_path_override_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit executable path replaces the bare `databricks` name."""
    cli = _install(monkeypatch)
    DatabricksSandboxLauncher(cli_path="/opt/databricks/bin/databricks").run("sb-1", "true")
    assert cli.argvs("ssh")[0][0] == "/opt/databricks/bin/databricks"


# ── run ─────────────────────────────────────────────────────


def test_run_passes_command_as_one_argument_after_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The remote command must ride as a SINGLE argv element after `--`.
    Splitting it on whitespace would let the sandbox's login shell see
    `echo` and `hi` as separate ssh arguments and break every command
    containing shell syntax.
    """
    cli = _install(monkeypatch)
    DatabricksSandboxLauncher().run("sb-1", "echo hi; exit 0")
    argv = cli.argvs("ssh")[0]
    assert argv[:4] == ["databricks", "sandbox", "ssh", "sb-1"]
    assert _remote_command(argv) == "echo hi; exit 0"


def test_run_reports_stdout_and_stderr_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Unlike the SDK-backed providers, this transport keeps the two
    streams apart — the launcher must not merge them into `stdout`.
    """
    cli = _install(monkeypatch)
    cli.reply_with("ssh", _Reply(stdout="out\n", stderr="err\n"))
    result = DatabricksSandboxLauncher().run("sb-1", "true")
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert result.returncode == 0


def test_run_raises_on_nonzero_when_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A checked failure names the sandbox, the exit code, and the command."""
    cli = _install(monkeypatch)
    cli.reply_with("ssh", _Reply(returncode=7, stderr="boom\n"))
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().run("sb-1", "false")
    assert "sb-1" in str(exc.value)
    assert "exit 7" in str(exc.value)
    assert "false" in str(exc.value)


def test_run_returns_failure_when_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
    """`check=False` reports the exit code instead of raising."""
    cli = _install(monkeypatch)
    cli.reply_with("ssh", _Reply(returncode=7))
    result = DatabricksSandboxLauncher().run("sb-1", "false", check=False)
    assert result.returncode == 7


def test_run_raises_install_hint_when_cli_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A missing binary surfacing mid-bootstrap (not just at `prepare`)
    must still carry the install remediation.
    """

    def _missing(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand in for an absent executable."""
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(dbx.subprocess, "run", _missing)
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().run("sb-1", "true")
    assert str(exc.value) == INSTALL_HINT


def test_run_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged SSH session must time out with a named action, not hang."""

    def _hang(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Stand in for a command that never returns."""
        raise subprocess.TimeoutExpired(cmd=argv, timeout=float(kwargs["timeout"]))

    monkeypatch.setattr(dbx.subprocess, "run", _hang)
    with pytest.raises(click.ClickException, match="Timed out"):
        DatabricksSandboxLauncher().run("sb-1", "true")


# ── put ─────────────────────────────────────────────────────


def test_put_streams_the_file_over_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    File shipping is `cat > <path>` fed from local stdin — verified as
    the working transport, since the CLI passes stdin through. The
    remote path must be shell-quoted: it lands in the sandbox's login
    shell, where an unquoted space or `;` would truncate or inject.
    """
    cli = _install(monkeypatch)
    local = tmp_path / "oa-wheels.tgz"
    local.write_bytes(b"wheel-bytes")
    DatabricksSandboxLauncher().put("sb-1", local, "/tmp/dir with space/oa-wheels.tgz")
    argv = cli.argvs("ssh")[0]
    assert _remote_command(argv) == "cat > '/tmp/dir with space/oa-wheels.tgz'"
    # The local file handle — not its bytes, not a path string — is what
    # the CLI child reads from.
    stdin = cli.calls[0].stdin
    assert stdin is not None
    assert stdin.name == str(local)


def test_put_raises_when_local_file_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unreadable local file must name the path, not the CLI."""
    _install(monkeypatch)
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().put("sb-1", tmp_path / "absent.tgz", "/tmp/x")
    assert "absent.tgz" in str(exc.value)


def test_put_raises_when_transfer_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A non-zero `cat` (e.g. a read-only destination) must fail loud."""
    cli = _install(monkeypatch)
    cli.reply_with("ssh", _Reply(returncode=1, stderr="Permission denied"))
    local = tmp_path / "f.tgz"
    local.write_bytes(b"x")
    with pytest.raises(click.ClickException) as exc:
        DatabricksSandboxLauncher().put("sb-1", local, "/root/f.tgz")
    assert "Permission denied" in str(exc.value)


# ── lifecycle ───────────────────────────────────────────────


def test_terminate_passes_auto_approve(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `delete` prompts interactively without `--auto-approve`, which would
    hang a server-side teardown forever. The flag is not optional.
    """
    cli = _install(monkeypatch)
    DatabricksSandboxLauncher().terminate("sb-1")
    assert cli.argvs("delete") == [["databricks", "sandbox", "delete", "sb-1", "--auto-approve"]]


def test_terminate_is_idempotent_for_a_missing_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Deleting an already-gone sandbox is success: the desired end state
    holds, and managed teardown races another cleanup path routinely.
    """
    cli = _install(monkeypatch)
    cli.reply_with("delete", _Reply(returncode=1, stderr="sandbox sb-1 not found"))
    DatabricksSandboxLauncher().terminate("sb-1")


def test_terminate_raises_on_other_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A delete that fails for any other reason must NOT be swallowed."""
    cli = _install(monkeypatch)
    cli.reply_with("delete", _Reply(returncode=1, stderr="permission denied"))
    with pytest.raises(click.ClickException, match="permission denied"):
        DatabricksSandboxLauncher().terminate("sb-1")


def test_resume_starts_the_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resume is `start <id>`, which blocks until the microVM is Running."""
    cli = _install(monkeypatch)
    DatabricksSandboxLauncher().resume("sb-1")
    assert cli.argvs("start") == [["databricks", "sandbox", "start", "sb-1"]]


def test_attach_starts_a_stopped_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A stopped sandbox retains its disk, so attaching revives it rather
    than rejecting it — the Daytona posture, not the Modal one.
    """
    cli = _install(monkeypatch)
    cli.reply_with("status", _Reply(stdout=json.dumps({"status": "Stopped"})))
    DatabricksSandboxLauncher().attach("sb-1")
    assert cli.argvs("start") == [["databricks", "sandbox", "start", "sb-1"]]


def test_attach_leaves_a_running_sandbox_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-running sandbox must not be restarted."""
    cli = _install(monkeypatch)
    cli.reply_with("status", _Reply(stdout=json.dumps({"status": "Running"})))
    DatabricksSandboxLauncher().attach("sb-1")
    assert cli.argvs("start") == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [("Running", True), ("Stopped", False), ("Stopping", False), ("Wedged", None)],
)
def test_is_running_maps_control_plane_status(
    monkeypatch: pytest.MonkeyPatch, status: str, expected: bool | None
) -> None:
    """
    Known states map to True/False; anything unrecognized maps to None
    so callers keep their existing liveness behavior instead of acting
    on a guess.
    """
    cli = _install(monkeypatch)
    cli.reply_with("status", _Reply(stdout=json.dumps({"status": status})))
    assert DatabricksSandboxLauncher().is_running("sb-1") is expected


def test_keep_alive_warns_instead_of_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Keep-alive is soft-fail per the launcher contract: a rejected policy
    leaves a usable sandbox that may reap itself, which is worth a
    warning but not an aborted bootstrap.
    """
    cli = _install(monkeypatch)
    cli.reply_with("config", _Reply(returncode=1, stderr="policy rejected"))
    DatabricksSandboxLauncher().keep_alive("sb-1")


# ── forward_local_port ──────────────────────────────────────


class _FakeSshForward:
    """
    Stand-in for the ``ssh -v -N -L`` child.

    Emits a scripted ``ssh -v`` transcript on stdout — readiness is read
    from that transcript, so the fake reproduces the real signal without
    binding a port or reaching a network.

    :param transcript: Lines the fake ssh prints, in order.
    :param returncode: Exit status once the transcript is exhausted, or
        ``None`` to stay alive like a healthy forward.
    """

    def __init__(self, transcript: list[str], returncode: int | None = None) -> None:
        self.stdout = io.StringIO("".join(transcript))
        self._returncode = returncode
        self.terminated = False

    @property
    def returncode(self) -> int | None:
        """Exit status, read by the launcher's error path."""
        return self._returncode

    def poll(self) -> int | None:
        """Report the child's exit status, if it has one."""
        return self._returncode

    def terminate(self) -> None:
        """Record the teardown signal."""
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        """Reap the fake child."""
        del timeout
        self._returncode = 0 if self._returncode is None else self._returncode
        return self._returncode

    def kill(self) -> None:
        """Nothing to kill — the fake never blocks on terminate."""


_READY_TRANSCRIPT = [
    "debug1: Authentication succeeded (publickey).\n",
    "debug1: Local forwarding listening on 127.0.0.1 port 8022.\n",
    "debug1: Entering interactive session.\n",
]


def test_forward_pins_both_endpoints_to_ipv4_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Both ends of the `-L` spec must be literal 127.0.0.1, never
    `localhost`.

    Unpinned, ssh binds the local port on BOTH ::1 and 127.0.0.1 while
    the sandbox side resolves `localhost` to whichever family it
    prefers — so a callback server bound only to IPv4 loopback can be
    missed entirely.

    The argv must also carry no `-o` option: the Databricks CLI
    re-quotes what it forwards to ssh, and `-o ExitOnForwardFailure=yes`
    was verified live to produce "Bad configuration option" while the
    single-token spelling silently dropped `-N`.
    """
    _install(monkeypatch)
    seen: list[list[str]] = []

    def _fake_popen(argv: list[str], **kwargs: Any) -> _FakeSshForward:
        """Record the argv and hand back a ready forward."""
        seen.append(argv)
        return _FakeSshForward(_READY_TRANSCRIPT)

    monkeypatch.setattr(dbx.subprocess, "Popen", _fake_popen)
    with DatabricksSandboxLauncher().forward_local_port("sb-1", 8022):
        pass
    argv = seen[0]
    assert "-L" in argv
    assert argv[argv.index("-L") + 1] == "127.0.0.1:8022:127.0.0.1:8022"
    assert "localhost" not in " ".join(argv)
    assert "-N" in argv
    # -v is what produces the readiness line the wait depends on.
    assert "-v" in argv
    # No `-o` options: the Databricks CLI re-quotes forwarded arguments
    # and mangles `-o Key=Value`, which cost a live debugging cycle when
    # `ExitOnForwardFailure` silently broke `-N`.
    assert "-o" not in argv
    assert not any(token.startswith("-o") and len(token) > 2 for token in argv)


def test_forward_waits_for_ssh_to_report_listening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Readiness comes from ssh's own transcript, and NOT from probing the
    port — both probe styles were observed to fail live.

    A *connect* probe traverses the tunnel and opens a real session
    against whatever listens in the sandbox; the App OAuth callback
    server accepts exactly ONE connection, so the probe consumed the very
    request the caller was waiting for. A *bind* probe races ssh for the
    port and, about one run in three, won — leaving ssh's own bind to
    fail and the tunnel dead while the port still looked taken.

    A transcript that never reports listening must therefore time out
    rather than be declared ready by a probe that touched the port.
    """
    _install(monkeypatch)
    monkeypatch.setattr(dbx, "_FORWARD_BIND_TIMEOUT_S", 0.5)
    monkeypatch.setattr(dbx, "_FORWARD_POLL_INTERVAL_S", 0.05)
    silent = ["debug1: Authentication succeeded (publickey).\n"]
    monkeypatch.setattr(dbx.subprocess, "Popen", lambda argv, **kwargs: _FakeSshForward(silent))
    with pytest.raises(click.ClickException, match="did not report listening"):
        with DatabricksSandboxLauncher().forward_local_port("sb-1", 8022):
            pass


def test_forward_fails_fast_when_ssh_cannot_listen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ssh reporting "cannot listen to port" must abort immediately rather
    than burn the full readiness timeout.

    This is the stand-in for `ExitOnForwardFailure=yes`, which the
    Databricks CLI cannot pass through intact. Without it, a port
    collision costs the caller the entire bind timeout before it learns
    anything.
    """
    _install(monkeypatch)
    # Deliberately generous: a slow path here would mean the failure was
    # detected by the deadline rather than by the transcript.
    monkeypatch.setattr(dbx, "_FORWARD_BIND_TIMEOUT_S", 30.0)
    monkeypatch.setattr(dbx, "_FORWARD_POLL_INTERVAL_S", 0.05)
    refused = [
        "debug1: Authentication succeeded (publickey).\n",
        "bind [127.0.0.1]:8022: Address already in use\n",
        "channel_setup_fwd_listener_tcpip: cannot listen to port: 8022\n",
    ]
    # Still alive — exactly the case ExitOnForwardFailure would have
    # turned into an exit, and the reason the transcript is watched.
    monkeypatch.setattr(dbx.subprocess, "Popen", lambda argv, **kwargs: _FakeSshForward(refused))
    started = time.monotonic()
    with pytest.raises(click.ClickException, match="could not listen"):
        with DatabricksSandboxLauncher().forward_local_port("sb-1", 8022):
            pass
    assert time.monotonic() - started < 10.0


def test_forward_reports_ssh_own_error_when_child_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An ssh that dies before listening for a reason OTHER than a bind
    failure (bad key, deleted sandbox, gateway refusal) must be reported
    with ssh's own words and exit code, not as a generic timeout the
    operator has to wait out.

    Distinct from the bind-failure path above: there the child is still
    alive and only the transcript reveals the problem; here the child is
    gone and its exit status is the evidence.
    """
    _install(monkeypatch)
    monkeypatch.setattr(dbx, "_FORWARD_BIND_TIMEOUT_S", 2.0)
    monkeypatch.setattr(dbx, "_FORWARD_POLL_INTERVAL_S", 0.05)
    failed = [
        "debug1: Offering public key: /home/me/.ssh/sandbox_ed25519\n",
        "ssh: Permission denied (publickey).\n",
    ]
    monkeypatch.setattr(
        dbx.subprocess, "Popen", lambda argv, **kwargs: _FakeSshForward(failed, returncode=255)
    )
    with pytest.raises(click.ClickException) as exc:
        with DatabricksSandboxLauncher().forward_local_port("sb-1", 8022):
            pass
    assert "Permission denied" in str(exc.value)
    assert "exit 255" in str(exc.value)


def test_forward_tears_down_the_child_on_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Leaving the block must stop ssh — a leaked forward holds the local
    port and keeps a billable SSH session open against the sandbox.
    """
    _install(monkeypatch)
    child = _FakeSshForward(_READY_TRANSCRIPT)
    monkeypatch.setattr(dbx.subprocess, "Popen", lambda argv, **kwargs: child)
    with DatabricksSandboxLauncher().forward_local_port("sb-1", 8022):
        pass
    assert child.terminated is True


# ── wheel install ───────────────────────────────────────────


def test_wheel_install_command_breaks_system_packages() -> None:
    """
    The sandbox image's interpreter is PEP 668 externally-managed, so a
    plain `pip install` refuses outright. `--force-reinstall` is equally
    load-bearing: the baked omnigent shares a version with the local
    build, so pip would otherwise report success and change nothing.
    """
    command = DatabricksSandboxLauncher().wheel_install_command("/tmp/oa-wheels.tgz")
    assert "--break-system-packages" in command
    assert "--force-reinstall" in command
    assert "--no-deps" in command
    assert "/tmp/oa-wheels.tgz" in command


def test_wheel_install_command_quotes_the_tarball_path() -> None:
    """The tarball path is interpolated into a shell command; quote it."""
    command = DatabricksSandboxLauncher().wheel_install_command("/tmp/a b.tgz")
    assert "'/tmp/a b.tgz'" in command
