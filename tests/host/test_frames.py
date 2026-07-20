"""Tests for host tunnel frame encode/decode."""

from __future__ import annotations

import json

import pytest

from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostCreateWorktreeFrame,
    HostCreateWorktreeResultFrame,
    HostFsRequestFrame,
    HostFsResultFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirEntry,
    HostListDirFrame,
    HostListDirResultFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    HostRemoveWorktreeFrame,
    HostRemoveWorktreeResultFrame,
    HostRunnerExitedFrame,
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)


def test_encode_injects_traceparent_under_active_span() -> None:
    """
    Encoding a host frame inside an active span stamps a W3C
    ``traceparent`` into the JSON envelope so the Host Daemon ↔ Server
    boundary joins the distributed trace; with no span active the wire
    form is unchanged (byte-for-byte wire-compatible), and decode always
    ignores the extra envelope key.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider

    frame = HostLaunchRunnerFrame(
        request_id="req_1",
        binding_token="tok",
        workspace="/tmp/ws",
        harness="claude-sdk",
    )

    # No active span: no traceparent added.
    assert "traceparent" not in json.loads(encode_host_frame(frame))

    provider = TracerProvider()
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    tracer = otel_trace.get_tracer("tests.host.frames")
    with tracer.start_as_current_span("rest"):
        encoded = encode_host_frame(frame)

    payload = json.loads(encoded)
    assert "traceparent" in payload
    # The extra envelope key must not break decoding back to the dataclass.
    decoded = decode_host_frame(encoded)
    assert isinstance(decoded, HostLaunchRunnerFrame)
    assert decoded.request_id == "req_1"
    assert decoded.workspace == "/tmp/ws"


def test_hello_frame_round_trip() -> None:
    """
    Verify HostHelloFrame survives encode → decode.

    If any field is dropped or garbled, the host tunnel would
    register with wrong capabilities or fail to reconcile
    runners on reconnect.
    """
    original = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="corey-laptop",
        runners=["runner_token_aaa", "runner_token_bbb"],
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.version == "0.1.0"
    assert decoded.frame_protocol_version == 1
    assert decoded.name == "corey-laptop"
    assert decoded.runners == ["runner_token_aaa", "runner_token_bbb"]


def test_hello_frame_empty_runners() -> None:
    """
    Verify HostHelloFrame with no runners decodes to an empty list.

    First connect has no runners; the field must default cleanly.
    """
    original = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="laptop",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.runners == []


def test_launch_runner_frame_round_trip() -> None:
    """
    Verify HostLaunchRunnerFrame survives encode → decode.

    If binding_token is garbled, the runner would connect with
    a wrong identity and the session binding would fail.
    """
    original = HostLaunchRunnerFrame(
        request_id="req_001",
        binding_token="secret_token_xyz",
        workspace="/Users/corey/projects/frontend",
        session_id="conv_abc123",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostLaunchRunnerFrame)
    assert decoded.request_id == "req_001"
    assert decoded.binding_token == "secret_token_xyz"
    assert decoded.workspace == "/Users/corey/projects/frontend"
    assert decoded.session_id == "conv_abc123"


def test_launch_runner_result_frame_success_round_trip() -> None:
    """
    Verify HostLaunchRunnerResultFrame (success) survives
    encode → decode.

    The server awaits this frame to confirm the runner was
    spawned. If status or runner_id is wrong, the binding
    flow stalls or binds the wrong runner.
    """
    original = HostLaunchRunnerResultFrame(
        request_id="req_001",
        status="launched",
        runner_id="runner_token_abc",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostLaunchRunnerResultFrame)
    assert decoded.request_id == "req_001"
    assert decoded.status == "launched"
    assert decoded.runner_id == "runner_token_abc"
    assert decoded.error is None


def test_launch_runner_result_frame_failure_round_trip() -> None:
    """
    Verify HostLaunchRunnerResultFrame (failure) preserves the
    error message.

    If error is dropped, the server can't report why the launch
    failed to the user.
    """
    original = HostLaunchRunnerResultFrame(
        request_id="req_001",
        status="failed",
        error="workspace path does not exist",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostLaunchRunnerResultFrame)
    assert decoded.status == "failed"
    assert decoded.runner_id is None
    assert decoded.error == "workspace path does not exist"


def test_hello_frame_configured_harnesses_round_trip() -> None:
    """
    Verify the hello frame's configured_harnesses map survives
    encode → decode with exact values.

    If a key or bool is dropped/garbled, the server would persist
    a wrong readiness map and the web picker would warn about the
    wrong harnesses (or miss a real warning).
    """
    original = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="corey-laptop",
        configured_harnesses={"claude-sdk": True, "codex": "needs-auth"},
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostHelloFrame)
    # Exact map equality: both the True and the False must survive —
    # False is the actionable "warn the user" value.
    assert decoded.configured_harnesses == {"claude-sdk": True, "codex": "needs-auth"}


def test_hello_frame_legacy_payload_decodes_unknown_harnesses() -> None:
    """
    Verify a hello payload from an OLDER host (no configured_harnesses
    key) decodes with the field as None — "unknown", never a dict.

    If this decoded to {} or raised, every pre-upgrade host would
    either fail its handshake or read as "nothing configured" and
    spuriously warn on all agents.
    """
    legacy = json.dumps(
        {
            "kind": "host.hello",
            "version": "0.1.0",
            "frame_protocol_version": 1,
            "name": "old-laptop",
            "runners": [],
        }
    )
    decoded = decode_host_frame(legacy)
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.configured_harnesses is None


def test_hello_frame_non_dict_configured_harnesses_decodes_as_none() -> None:
    """
    Verify a malformed configured_harnesses value (not a JSON object)
    decodes as None instead of raising.

    The hello is the handshake frame — a peer sending a bad value for
    this advisory field must not break the whole tunnel connection.
    """
    malformed = json.dumps(
        {
            "kind": "host.hello",
            "version": "0.1.0",
            "frame_protocol_version": 1,
            "name": "laptop",
            "runners": [],
            "configured_harnesses": ["claude-sdk"],
        }
    )
    decoded = decode_host_frame(malformed)
    assert isinstance(decoded, HostHelloFrame)
    assert decoded.configured_harnesses is None


def test_launch_runner_frame_harness_round_trip() -> None:
    """
    Verify the launch frame's harness field survives encode → decode.

    If harness is dropped, the host's pre-spawn configuration check
    silently never runs (None skips it) and unconfigured launches
    regress to dying inside the executor.
    """
    original = HostLaunchRunnerFrame(
        request_id="req_001",
        binding_token="secret_token_xyz",
        workspace="/Users/corey/projects/frontend",
        harness="claude-sdk",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostLaunchRunnerFrame)
    assert decoded.harness == "claude-sdk"


def test_launch_runner_frame_legacy_payload_decodes_harness_none() -> None:
    """
    Verify a launch payload from an OLDER server (no harness key)
    decodes with harness=None so the host skips the check (fail open).

    If this raised, a new host could not serve launches from an
    older server at all.
    """
    legacy = json.dumps(
        {
            "kind": "host.launch_runner",
            "request_id": "req_001",
            "binding_token": "tok",
            "workspace": "/w",
        }
    )
    decoded = decode_host_frame(legacy)
    assert isinstance(decoded, HostLaunchRunnerFrame)
    assert decoded.session_id is None
    assert decoded.harness is None


def test_launch_runner_result_frame_error_code_round_trip() -> None:
    """
    Verify the result frame's error_code survives encode → decode.

    The server keys its 412 mapping on this exact string — if it's
    dropped, an unconfigured-harness refusal degrades to the generic
    warn-and-return-200 path and the user never sees the
    `omnigent setup` recommendation.
    """
    original = HostLaunchRunnerResultFrame(
        request_id="req_001",
        status="failed",
        error="harness 'codex' is not configured",
        error_code=HARNESS_NOT_CONFIGURED_ERROR_CODE,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostLaunchRunnerResultFrame)
    assert decoded.error_code == "harness_not_configured"
    assert decoded.error == "harness 'codex' is not configured"


def test_launch_runner_result_frame_legacy_payload_decodes_error_code_none() -> None:
    """
    Verify a result payload from an OLDER host (no error_code key)
    decodes with error_code=None.

    None must mean "uncategorized failure" so the server keeps the
    existing generic failure handling for pre-upgrade hosts.
    """
    legacy = json.dumps(
        {
            "kind": "host.launch_runner_result",
            "request_id": "req_001",
            "status": "failed",
            "error": "boom",
        }
    )
    decoded = decode_host_frame(legacy)
    assert isinstance(decoded, HostLaunchRunnerResultFrame)
    assert decoded.error_code is None


def test_stop_runner_frame_round_trip() -> None:
    """
    Verify HostStopRunnerFrame survives encode → decode.

    If runner_id is garbled, the host would kill the wrong
    process.
    """
    original = HostStopRunnerFrame(
        request_id="req_002",
        runner_id="runner_token_abc",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStopRunnerFrame)
    assert decoded.request_id == "req_002"
    assert decoded.runner_id == "runner_token_abc"


def test_stop_runner_result_frame_round_trip() -> None:
    """
    Verify HostStopRunnerResultFrame survives encode → decode.
    """
    original = HostStopRunnerResultFrame(
        request_id="req_002",
        status="stopped",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStopRunnerResultFrame)
    assert decoded.request_id == "req_002"
    assert decoded.status == "stopped"
    assert decoded.error is None


def test_runner_exited_frame_round_trip() -> None:
    """
    Verify HostRunnerExitedFrame survives encode → decode.

    This frame carries the failure cause (exit code + log tail) from
    the host daemon to the server. A lossy round-trip here means a
    crashed runner's error is mangled or dropped before it ever
    reaches the waiting client.
    """
    original = HostRunnerExitedFrame(
        runner_id="runner_abc123",
        error=(
            "runner process exited with code 1 (log on host: ~/x.log)\n"
            "--- runner log tail ---\nRuntimeError: boom"
        ),
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostRunnerExitedFrame)
    assert decoded.runner_id == "runner_abc123"
    # The multi-line error (including the log tail) must survive intact.
    assert decoded.error == original.error


def test_runner_exited_frame_missing_error_raises() -> None:
    """
    Verify a runner_exited frame without ``error`` fails to decode.

    ``error`` is the entire payload of this report — accepting a frame
    without it would record an empty cause and the client would fail
    with a blank message.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame('{"kind": "host.runner_exited", "runner_id": "runner_abc123"}')


def test_runner_status_frame_round_trip() -> None:
    """
    Verify HostRunnerStatusFrame survives encode → decode.

    A garbled runner_id here would make the host answer about the wrong
    runner, so the dispatch path could wait for (or relaunch) the wrong
    one.
    """
    original = HostRunnerStatusFrame(
        request_id="req_rs_1",
        runner_id="runner_token_abc",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostRunnerStatusFrame)
    assert decoded.request_id == "req_rs_1"
    assert decoded.runner_id == "runner_token_abc"


def test_runner_status_result_frame_round_trip() -> None:
    """
    Verify HostRunnerStatusResultFrame survives encode → decode for each
    verdict.

    ``status`` is the entire signal the dispatch gate acts on — a lossy
    round-trip would make the server wait when it should relaunch, or
    vice versa.
    """
    for status in ("alive", "dead", "unknown"):
        original = HostRunnerStatusResultFrame(
            request_id="req_rs_1",
            status=status,
        )
        decoded = decode_host_frame(encode_host_frame(original))
        assert isinstance(decoded, HostRunnerStatusResultFrame)
        assert decoded.request_id == "req_rs_1"
        assert decoded.status == status


def test_decode_unknown_kind_raises() -> None:
    """
    Verify that an unknown frame kind raises ValueError.

    The host tunnel must reject frames it doesn't understand
    rather than silently ignoring them.
    """
    with pytest.raises(ValueError, match="unknown host frame kind"):
        decode_host_frame('{"kind": "host.unknown_frame"}')


def test_decode_missing_kind_raises() -> None:
    """
    Verify that a frame without a ``kind`` field raises ValueError.

    A kindless frame is malformed — it must not parse as any frame
    type.
    """
    with pytest.raises(ValueError, match="missing 'kind' field"):
        decode_host_frame('{"version": "0.1.0"}')


def test_decode_missing_required_field_raises() -> None:
    """
    Verify that a frame missing a required field raises ValueError.

    If required fields aren't validated, a frame with missing data
    would create a dataclass with None where a str is expected,
    causing downstream crashes.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame('{"kind": "host.hello", "frame_protocol_version": 1, "name": "laptop"}')


def test_decode_invalid_json_raises() -> None:
    """
    Verify that malformed JSON raises ValueError.
    """
    with pytest.raises(ValueError, match="not valid JSON"):
        decode_host_frame("not json at all")


def test_encode_unknown_type_raises() -> None:
    """
    Verify that encoding an unknown frame type raises TypeError.
    """
    with pytest.raises(TypeError, match="unknown host frame type"):
        encode_host_frame("not a frame")  # type: ignore[arg-type]


# ── host.stat frames ────────────────────────────────────


def test_stat_frame_round_trip() -> None:
    """
    Verify HostStatFrame request frame survives encode → decode.

    Pins the wire shape that session-create validation relies on:
    a single ``path`` field that may be absolute or tilde-prefixed.
    If this field name or type drifts, the validation flow can't
    talk to the host.
    """
    original = HostStatFrame(request_id="req_stat_1", path="/Users/corey/universe")
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStatFrame)
    assert decoded.request_id == "req_stat_1"
    assert decoded.path == "/Users/corey/universe"


def test_stat_frame_accepts_tilde_path() -> None:
    """
    Verify HostStatFrame round-trips a tilde-prefixed path verbatim.

    The host (not the server) is the source of truth for ``~``
    expansion — see designs/SESSION_WORKSPACE_SELECTION.md. The
    frame must therefore preserve tildes through the wire so the
    host's stat handler can do the expansion. If the encoder
    silently expands tildes, server-side resolution would diverge
    from the host's process owner.
    """
    original = HostStatFrame(request_id="req_stat_tilde", path="~/projects")
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStatFrame)
    assert decoded.path == "~/projects"


def test_stat_result_directory_round_trip() -> None:
    """
    Verify HostStatResultFrame for an existing directory survives
    encode → decode.

    Three properties matter for the server-side validator:
    ``exists`` is True, ``type`` is ``"directory"``, and
    ``canonical_path`` carries the realpath. Validation step 4
    (workspace boundary check) operates on canonical_path; if
    that field is dropped, every host-launched session would be
    rejected as "outside boundary."
    """
    original = HostStatResultFrame(
        request_id="req_stat_2",
        status="ok",
        exists=True,
        type="directory",
        canonical_path="/Users/corey/universe",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStatResultFrame)
    assert decoded.request_id == "req_stat_2"
    assert decoded.status == "ok"
    assert decoded.exists is True
    assert decoded.type == "directory"
    assert decoded.canonical_path == "/Users/corey/universe"
    assert decoded.error is None


def test_stat_result_missing_path_round_trip() -> None:
    """
    Verify HostStatResultFrame for a non-existent path round-trips.

    When ``exists`` is False, ``type`` and ``canonical_path`` must
    both be None. If a stale ``canonical_path`` carried over (e.g.
    from the input path), the server might store a session row
    pointing at a phantom directory — exactly the orphan-session
    scenario session-create validation is meant to prevent.
    """
    original = HostStatResultFrame(
        request_id="req_stat_3",
        status="ok",
        exists=False,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStatResultFrame)
    assert decoded.exists is False
    assert decoded.type is None
    assert decoded.canonical_path is None
    assert decoded.error is None


def test_stat_result_failed_round_trip() -> None:
    """
    Verify HostStatResultFrame survives encode → decode for I/O failures.

    ``status: "failed"`` is reserved for unexpected errors (EIO,
    etc.). EACCES and ENOENT both fold into ``status: "ok",
    exists: false`` per the design. The error message must
    survive so the server can surface it.
    """
    original = HostStatResultFrame(
        request_id="req_stat_4",
        status="failed",
        exists=False,
        error="I/O error reading filesystem",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostStatResultFrame)
    assert decoded.status == "failed"
    assert decoded.error == "I/O error reading filesystem"


def test_stat_result_missing_exists_field_raises() -> None:
    """
    Verify that decoding a stat_result without ``exists`` raises
    ValueError.

    ``exists`` is the load-bearing bit for validation. A frame
    that omits it would cause silent ``False`` defaulting and
    every legitimate path would fail validation. Decoding must
    fail loud instead.
    """
    with pytest.raises(ValueError, match="missing required bool field"):
        decode_host_frame('{"kind": "host.stat_result", "request_id": "r", "status": "ok"}')


def test_stat_request_missing_path_raises() -> None:
    """
    Verify that decoding a stat request without ``path`` raises
    ValueError.

    Without ``path`` the host has no way to know what to stat;
    a default-to-empty would silently stat the host process's cwd
    and return misleading data. Failing loud preserves safety.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame('{"kind": "host.stat", "request_id": "r"}')


# ── host.list_dir frames ────────────────────────────────


def test_list_dir_frame_round_trip() -> None:
    """
    Verify HostListDirFrame request frame survives encode → decode.

    Pins the wire shape used by the directory picker: ``path`` plus
    pagination fields (``limit`` / ``after`` / ``before``).
    """
    original = HostListDirFrame(
        request_id="req_list_1",
        path="/Users/corey/projects",
        limit=20,
        after=None,
        before=None,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirFrame)
    assert decoded.request_id == "req_list_1"
    assert decoded.path == "/Users/corey/projects"
    assert decoded.limit == 20
    assert decoded.after is None
    assert decoded.before is None


def test_list_dir_frame_with_pagination_cursors() -> None:
    """
    Verify pagination cursors round-trip.

    Without round-tripping, the Web UI's "next page" / "prev page"
    cursors would silently degrade (always returning the first page).
    """
    original = HostListDirFrame(
        request_id="req_list_2",
        path="/foo",
        limit=10,
        after="/foo/m",
        before=None,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirFrame)
    assert decoded.limit == 10
    assert decoded.after == "/foo/m"


def test_list_dir_frame_accepts_tilde_path() -> None:
    """
    Verify a tilde-prefixed path round-trips verbatim.

    The host (not the server) is the source of truth for ``~`` —
    same rules as host.stat. The frame must therefore preserve
    tildes through the wire so the host's list_dir handler can
    expand against its own process owner.
    """
    original = HostListDirFrame(request_id="req_list_tilde", path="~/projects")
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirFrame)
    assert decoded.path == "~/projects"


def test_list_dir_result_round_trip() -> None:
    """
    Verify HostListDirResultFrame survives encode → decode with
    multiple entry types.

    Each entry must carry name, absolute path, type, optional bytes,
    and modified_at. If any field is dropped or mis-typed, the Web
    UI's tree view would render with missing data.
    """
    original = HostListDirResultFrame(
        request_id="req_list_3",
        status="ok",
        entries=[
            HostListDirEntry(
                name="src",
                path="/Users/corey/foo/src",
                type="directory",
                bytes=None,
                modified_at=1779980000,
            ),
            HostListDirEntry(
                name="README.md",
                path="/Users/corey/foo/README.md",
                type="file",
                bytes=1234,
                modified_at=1779980100,
            ),
        ],
        has_more=False,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirResultFrame)
    assert decoded.status == "ok"
    assert decoded.has_more is False
    assert len(decoded.entries) == 2
    assert decoded.entries[0].type == "directory"
    assert decoded.entries[0].bytes is None
    assert decoded.entries[1].type == "file"
    assert decoded.entries[1].bytes == 1234


def test_list_dir_result_empty_entries_round_trip() -> None:
    """
    Verify a result with an empty entry list round-trips.

    Empty directories are common (a fresh project, a checkout
    clean state). If the encoder drops empty arrays, the Web UI
    would crash trying to iterate ``None``.
    """
    original = HostListDirResultFrame(
        request_id="req_list_empty",
        status="ok",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirResultFrame)
    assert decoded.entries == []
    assert decoded.has_more is False


def test_list_dir_result_failed_round_trip() -> None:
    """
    Verify a failure status survives encode → decode with the error
    message intact.

    Without the error message, the route layer can't surface
    ``"path does not exist"`` etc. to the user — it would have to
    fall back to a generic 500.
    """
    original = HostListDirResultFrame(
        request_id="req_list_fail",
        status="failed",
        error="scandir failed: I/O error",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListDirResultFrame)
    assert decoded.status == "failed"
    assert decoded.error == "scandir failed: I/O error"
    assert decoded.entries == []


def test_list_dir_request_missing_path_raises() -> None:
    """
    Verify decoding a list_dir without ``path`` raises ValueError.

    Without ``path`` the host has nothing to list; a default-to-cwd
    fallback would silently stat the host process's working dir
    and return misleading data. Fail loud instead.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame('{"kind": "host.list_dir", "request_id": "r"}')


def test_list_dir_result_entry_missing_modified_at_raises() -> None:
    """
    Verify each entry must have a ``modified_at`` integer.

    The Web UI sorts entries by mtime; a missing field would make
    the sort silently inconsistent across pages.
    """
    bad = (
        '{"kind": "host.list_dir_result", "request_id": "r", "status": "ok", '
        '"entries": [{"name": "x", "path": "/x", "type": "file", "bytes": 1}], '
        '"has_more": false}'
    )
    with pytest.raises(ValueError, match="modified_at"):
        decode_host_frame(bad)


def test_create_worktree_frame_round_trip() -> None:
    """Verify HostCreateWorktreeFrame survives encode → decode.

    A garbled repo_path or branch_name would create the worktree in
    the wrong place or with the wrong branch.
    """
    original = HostCreateWorktreeFrame(
        request_id="req_wt_1",
        repo_path="/Users/alice/myrepo",
        branch_name="feature/login",
        base_branch="main",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateWorktreeFrame)
    assert decoded == original


def test_create_worktree_frame_optional_base_defaults_none() -> None:
    """Verify base_branch is nullable and round-trips as None."""
    original = HostCreateWorktreeFrame(
        request_id="req_wt_2",
        repo_path="/repo",
        branch_name="wip",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateWorktreeFrame)
    assert decoded.base_branch is None


def test_create_worktree_result_frame_round_trip() -> None:
    """Verify HostCreateWorktreeResultFrame survives encode → decode.

    The server stores worktree_path as the session workspace; a
    dropped field would persist a session with no workspace.
    """
    original = HostCreateWorktreeResultFrame(
        request_id="req_wt_1",
        status="ok",
        worktree_path="/Users/alice/myrepo-worktrees/feature-login",
        branch="feature/login",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateWorktreeResultFrame)
    assert decoded == original


def test_create_worktree_result_frame_failure_round_trip() -> None:
    """Verify a failed create-worktree result carries its error."""
    original = HostCreateWorktreeResultFrame(
        request_id="req_wt_1",
        status="failed",
        error="branch 'x' already exists",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateWorktreeResultFrame)
    assert decoded.worktree_path is None
    assert decoded.error == "branch 'x' already exists"


def test_remove_worktree_frame_round_trip() -> None:
    """Verify HostRemoveWorktreeFrame survives encode → decode.

    A dropped delete_branch flag would silently change cleanup
    behavior (delete the branch when the user didn't ask, or vice
    versa).
    """
    original = HostRemoveWorktreeFrame(
        request_id="req_rm_1",
        worktree_path="/Users/alice/myrepo-worktrees/feature-login",
        branch="feature/login",
        delete_branch=True,
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostRemoveWorktreeFrame)
    assert decoded == original


def test_remove_worktree_frame_non_bool_delete_branch_raises() -> None:
    """Verify a non-bool delete_branch is rejected, not coerced.

    Coercing a truthy string to True would delete a branch the user
    didn't ask to delete.
    """
    bad = (
        '{"kind": "host.remove_worktree", "request_id": "r", '
        '"worktree_path": "/x", "delete_branch": "yes"}'
    )
    with pytest.raises(ValueError, match="delete_branch"):
        decode_host_frame(bad)


def test_remove_worktree_result_frame_round_trip() -> None:
    """Verify HostRemoveWorktreeResultFrame survives encode → decode."""
    original = HostRemoveWorktreeResultFrame(request_id="req_rm_1", status="ok")
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostRemoveWorktreeResultFrame)
    assert decoded == original


# ── host.list_worktrees frames ──────────────────────────


def test_list_worktrees_frame_round_trip() -> None:
    """Verify HostListWorktreesFrame survives encode → decode.

    A garbled repo_path would list the wrong repository's worktrees.
    """
    original = HostListWorktreesFrame(
        request_id="req_wt_ls_1",
        repo_path="/Users/alice/myrepo",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListWorktreesFrame)
    assert decoded == original


def test_list_worktrees_result_frame_round_trip() -> None:
    """Verify HostListWorktreesResultFrame survives encode → decode.

    The worktree dicts feed the picker; a dropped or reshaped field
    would break branch prefill / start-in-worktree selection.
    """
    original = HostListWorktreesResultFrame(
        request_id="req_wt_ls_1",
        status="ok",
        worktrees=[
            {"path": "/Users/alice/myrepo", "branch": "main", "is_main": True, "detached": False},
            {
                "path": "/Users/alice/myrepo-worktrees/feature-login",
                "branch": "feature/login",
                "is_main": False,
                "detached": False,
            },
        ],
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListWorktreesResultFrame)
    assert decoded == original


def test_list_worktrees_result_frame_failure_round_trip() -> None:
    """Verify a failed list-worktrees result carries its error and null list."""
    original = HostListWorktreesResultFrame(
        request_id="req_wt_ls_1",
        status="failed",
        error="not a git repository",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostListWorktreesResultFrame)
    assert decoded.worktrees is None
    assert decoded.error == "not a git repository"


def test_list_worktrees_result_frame_rejects_non_list() -> None:
    """A non-list ``worktrees`` field is rejected, not coerced."""
    bad = (
        '{"kind": "host.list_worktrees_result", "request_id": "r", '
        '"status": "ok", "worktrees": "nope"}'
    )
    with pytest.raises(ValueError, match="worktrees"):
        decode_host_frame(bad)


# ── host.create_dir frames ──────────────────────────────


def test_create_dir_frame_round_trip() -> None:
    """
    Verify HostCreateDirFrame request frame survives encode → decode.

    Pins the wire shape used by the picker's "New folder" action:
    ``request_id`` plus the directory ``path`` to create.
    """
    original = HostCreateDirFrame(
        request_id="req_mkdir_1",
        path="/Users/corey/projects/new-app",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateDirFrame)
    assert decoded == original


def test_create_dir_frame_accepts_tilde_path() -> None:
    """
    Verify a tilde-prefixed path round-trips verbatim.

    The host (not the server) expands ``~``, same rules as
    ``host.list_dir`` — so the tilde must survive the wire.
    """
    original = HostCreateDirFrame(request_id="req_mkdir_tilde", path="~/scratch")
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateDirFrame)
    assert decoded.path == "~/scratch"


def test_create_dir_request_missing_path_raises() -> None:
    """
    Verify decoding a create_dir without ``path`` raises ValueError.

    Without ``path`` the host has nothing to create; failing loud
    beats silently creating something under the process cwd.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame('{"kind": "host.create_dir", "request_id": "r"}')


def test_create_dir_result_success_round_trip() -> None:
    """
    Verify a successful create-dir result round-trips with the created
    absolute path intact.

    The picker navigates into ``path`` after creating it; a dropped
    field would leave the user staring at the old directory.
    """
    original = HostCreateDirResultFrame(
        request_id="req_mkdir_2",
        status="ok",
        path="/Users/corey/projects/new-app",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateDirResultFrame)
    assert decoded == original


def test_create_dir_result_error_round_trip() -> None:
    """
    Verify an expected filesystem error round-trips with the message
    intact and ``path`` left ``None``.

    The route maps a non-empty ``error`` to a 409 so the picker can
    show "directory already exists" — that hinges on the message
    surviving the wire.
    """
    original = HostCreateDirResultFrame(
        request_id="req_mkdir_3",
        status="ok",
        error="directory already exists",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostCreateDirResultFrame)
    assert decoded.status == "ok"
    assert decoded.path is None
    assert decoded.error == "directory already exists"


def test_fs_request_round_trip() -> None:
    """
    Verify an fs request round-trips with op, workspace, session, and params.

    The host reads a session's workspace from disk to serve the file panel
    when the runner is offline; a dropped ``params`` would silently read the
    root / default page instead of what the client asked for.
    """
    original = HostFsRequestFrame(
        request_id="req_fs_1",
        op="list_or_read",
        workspace="/Users/corey/project",
        session_id="conv_abc123",
        params={"path": "src", "limit": 100, "order": "asc"},
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostFsRequestFrame)
    assert decoded == original


def test_fs_request_defaults_empty_params() -> None:
    """
    Verify an fs request with no ``params`` decodes to an empty dict.

    The ``changes`` op carries no params; it must not fail to decode nor
    surface ``None`` where the handler expects a dict.
    """
    decoded = decode_host_frame(
        '{"kind": "host.fs_request", "request_id": "r", "op": "changes", '
        '"workspace": "/w", "session_id": "s"}'
    )
    assert isinstance(decoded, HostFsRequestFrame)
    assert decoded.params == {}


def test_fs_request_missing_op_raises() -> None:
    """
    Verify decoding an fs request without ``op`` raises ValueError.

    Without an op the host cannot route the read; failing loud beats
    guessing an operation.
    """
    with pytest.raises(ValueError, match="missing required string field"):
        decode_host_frame(
            '{"kind": "host.fs_request", "request_id": "r", "workspace": "/w", "session_id": "s"}'
        )


def test_fs_request_non_object_params_raises() -> None:
    """
    Verify a non-object ``params`` is rejected rather than silently dropped.

    A malformed peer sending ``params: []`` should fail decode instead of
    reaching the handler as the wrong type.
    """
    with pytest.raises(ValueError, match="must be a JSON object: 'params'"):
        decode_host_frame(
            '{"kind": "host.fs_request", "request_id": "r", "op": "changes", '
            '"workspace": "/w", "session_id": "s", "params": []}'
        )


def test_fs_result_success_round_trip() -> None:
    """
    Verify a successful fs result round-trips with the payload intact.

    The payload is the runner-shaped JSON the panel renders; a dropped
    field would blank the viewer even though the host read succeeded.
    """
    original = HostFsResultFrame(
        request_id="req_fs_2",
        status="ok",
        payload={"object": "list", "data": [], "has_more": False},
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostFsResultFrame)
    assert decoded == original


def test_fs_result_error_round_trip() -> None:
    """
    Verify an fs error result round-trips with the status/code/message.

    The server reproduces the runner's HTTP response from these fields; a
    404 that arrived as a 500 would mislead the client's error handling.
    """
    original = HostFsResultFrame(
        request_id="req_fs_3",
        status="error",
        error_status=404,
        error_code="not_found",
        error="Path 'missing.py' not found",
    )
    decoded = decode_host_frame(encode_host_frame(original))
    assert isinstance(decoded, HostFsResultFrame)
    assert decoded == original


def test_fs_result_null_payload_round_trip() -> None:
    """
    Verify an fs result with a null payload decodes cleanly.

    Error results carry no payload; ``null`` must not be coerced into a
    dict nor raise during decode.
    """
    decoded = decode_host_frame(
        '{"kind": "host.fs_result", "request_id": "r", "status": "error", '
        '"payload": null, "error_status": 500, "error_code": "boom", "error": "x"}'
    )
    assert isinstance(decoded, HostFsResultFrame)
    assert decoded.payload is None
    assert decoded.error_status == 500
