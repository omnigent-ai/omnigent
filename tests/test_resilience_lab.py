"""Mocked readiness and failure-evidence checks for native resilience labs."""

from __future__ import annotations

import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import yaml

from tests._helpers.server_runner import ServerRunner
from tests.e2e.resilience import conftest as resilience_fixtures
from tests.e2e.resilience.lab import lab as lab_module
from tests.e2e.resilience.lab.events import EventLog
from tests.e2e.resilience.lab.lab import Lab, LabConfig
from tests.e2e.resilience.lab.observe import Observation, SessionWatcher
from tests.e2e.resilience.scenarios import _contract


@pytest.mark.parametrize(
    "helper,scenario",
    [
        pytest.param("lab", "rpc-delay", id="lab"),
        pytest.param("lab", "rpc-timeout", id="lab-rpc-timeout-once"),
        pytest.param("server-runner", "rpc-delay", id="server-runner"),
        pytest.param("server-runner", "rpc-timeout", id="server-runner-rpc-timeout-once"),
        pytest.param("server-runner", "unrelated-host", id="server-runner-unrelated-host"),
        pytest.param("server-runner", "deadline", id="server-runner-deadline"),
        pytest.param("lab", "remaining-budget", id="lab-rpc-remaining-budget"),
        pytest.param("lab", "late-success", id="lab-rpc-late-success"),
        pytest.param("lab", "final-sleep-budget", id="lab-final-sleep-budget"),
        pytest.param("server-runner", "remaining-budget", id="server-runner-rpc-remaining-budget"),
        pytest.param("server-runner", "late-success", id="server-runner-rpc-late-success"),
        pytest.param("lab", "host-exit", id="lab-host-exit"),
        pytest.param("server-runner", "host-exit", id="server-runner-host-exit"),
    ],
)
def test_host_startup_waits_for_rpc_when_online_row_precedes_tunnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, helper: str, scenario: str
) -> None:
    host_id = "a" * 32
    rpc_attempted = False
    rpc_ready = False
    clock = SimpleNamespace(now=0.0)
    spawned_env = {}
    request_timeouts = []
    rpc_timeouts = []

    def advance_clock(seconds: float) -> None:
        clock.now += seconds if scenario == "final-sleep-budget" else max(seconds, 0.1)

    sleep = Mock(side_effect=advance_clock)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rpc_attempted, rpc_ready
        assert request.method == "GET"
        request_timeouts.append(request.extensions["timeout"])
        if request.url.path == "/v1/hosts":
            if scenario == "deadline":
                clock.now = 1.0
            # The database commit can be visible before the tunnel is registered.
            rows = [{"host_id": host_id, "status": "online"}]
            if scenario == "unrelated-host":
                rows.insert(0, {"host_id": "unrelated-host", "status": "online"})
            return httpx.Response(200, json={"hosts": rows})
        if request.url.path == "/v1/hosts/unrelated-host/filesystem":
            return httpx.Response(200, json={"data": []})
        assert request.url.path == f"/v1/hosts/{host_id}/filesystem"
        assert request.url.params["limit"] == "1"
        rpc_timeouts.append(request.extensions["timeout"])
        if scenario == "deadline":
            rpc_ready = True
            return httpx.Response(200, json={"data": []})
        if not rpc_attempted:
            rpc_attempted = True
            if scenario == "rpc-timeout":
                raise httpx.ReadTimeout("host tunnel is not ready", request=request)
            if scenario in {"remaining-budget", "late-success"}:
                clock.now = 0.75
            if scenario == "final-sleep-budget":
                clock.now = 0.99
            return httpx.Response(
                409, json={"error": {"code": "conflict", "message": "host is offline"}}
            )
        rpc_ready = True
        if scenario == "late-success":
            clock.now = 1.1
        return httpx.Response(200, json={"object": "list", "data": [], "has_more": False})

    with ExitStack() as resources:
        observer = resources.enter_context(
            httpx.Client(transport=httpx.MockTransport(respond), base_url="http://test")
        )
        if helper == "lab":
            # Readiness needs no constructor sockets, TLS material, or process cleanup.
            lab = Lab.__new__(Lab)
            lab.host_id = host_id
            lab._proxies = Mock(host=Mock(url="http://test"))
            monkeypatch.setattr(lab, "_host_side_env", dict)
            monkeypatch.setattr(
                lab,
                "_spawn",
                lambda *_: Mock(
                    running=scenario != "host-exit", log_tail=lambda: "host-startup-marker"
                ),
            )
            lab.observer = observer
            monkeypatch.setattr(
                "tests.e2e.resilience.lab.lab.time",
                SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
            )
            monkeypatch.setattr(
                "tests.e2e.resilience.lab.lab._POLL_S",
                0.25 if scenario == "final-sleep-budget" else 0,
            )
            if scenario in {"remaining-budget", "late-success", "final-sleep-budget"}:
                monkeypatch.setattr("tests.e2e.resilience.lab.lab._STARTUP_TIMEOUT_S", 1.0)
            start_host = lab._start_host
        else:
            monkeypatch.setattr("tests._helpers.server_runner.find_free_port", lambda: 1)
            stack = ServerRunner(
                tmp_path / "stack",
                resources,
                server_bootstrap=None,
                server_env={},
                base_env={},
                server_cwd=None,
                workspace=None,
                database_uri=None,
                artifact_location=None,
                binding_token="example-token",
                health_timeout=1,
                poll_interval=0,
            )
            stack.host_id = host_id

            def spawn(*args, **kwargs):
                spawned_env.update(args[3])
                return Mock(
                    poll=lambda: 23 if scenario == "host-exit" else None,
                    returncode=23 if scenario == "host-exit" else None,
                )

            monkeypatch.setattr(stack, "_client", observer)
            monkeypatch.setattr(stack, "_spawn", spawn)
            if scenario == "host-exit":
                stack.log_path("host").write_text("host-startup-marker")
            monkeypatch.setattr(
                "tests._helpers.server_runner.time",
                SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
            )
            start_host = stack.start_host
        if scenario == "host-exit":
            error = RuntimeError if helper == "lab" else AssertionError
            with pytest.raises(error, match="exited") as raised:
                start_host()
            assert "host-startup-marker" in str(raised.value)
            assert not request_timeouts, "An exited host must fail before any readiness request"
            sleep.assert_not_called()
        elif scenario in {"deadline", "late-success", "final-sleep-budget"}:
            error = TimeoutError if helper == "lab" else AssertionError
            with pytest.raises(error, match=r"host daemon|never became ready"):
                start_host()
            if scenario == "deadline":
                assert not rpc_ready, "No host RPC may start after the readiness deadline"
                assert request_timeouts[0]["read"] <= 1.0
            elif scenario == "final-sleep-budget":
                assert not rpc_ready
                assert len(request_timeouts) == len(rpc_timeouts) == 1
                sleep.assert_called_once_with(pytest.approx(0.01))
                assert clock.now == pytest.approx(1.0)
            else:
                assert rpc_ready, "The deadline must reject the late successful RPC"
        else:
            start_host()
            assert rpc_ready, "Host startup returned before the host could answer an RPC"
        if scenario == "rpc-timeout":
            assert len(rpc_timeouts) == 2
        if scenario in {"remaining-budget", "late-success"}:
            assert len(rpc_timeouts) == 2
            assert 0 < rpc_timeouts[0]["read"] <= 1.0
            assert 0 < rpc_timeouts[1]["read"] <= 0.25
        if helper == "server-runner":
            assert spawned_env["OMNIGENT_HOST_ID"] == host_id
            assert spawned_env["OMNIGENT_HOST_NAME"]


def test_runner_readiness_stops_after_health_exhausts_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = SimpleNamespace(now=0.0)
    requests = []
    sleep = Mock()

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, request.extensions["timeout"]))
        assert request.url.path == "/health", "Runner status started after the health deadline"
        clock.now = 1.0
        return httpx.Response(200, json={"status": "ok"})

    stack = ServerRunner.__new__(ServerRunner)
    stack.base_url = "http://test"
    stack.runner_id = "example-runner"
    stack.server = stack.runner = stack.host = None
    stack._health_timeout = 1.0
    stack._poll_interval = 0.0
    monkeypatch.setattr(stack, "log_tail", lambda: "runner-readiness-marker")
    monkeypatch.setattr(
        "tests._helpers.server_runner.time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep),
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        stack._client = client
        with pytest.raises(AssertionError, match="never became ready") as raised:
            stack._wait_ready(runner=True)

    assert "runner-readiness-marker" in str(raised.value)
    assert [path for path, _ in requests] == ["/health"]
    assert requests[0][1]["read"] == 1.0
    assert clock.now == 1.0
    sleep.assert_not_called()


@pytest.mark.parametrize(
    "outcome,keep",
    [
        ("passed", False),
        ("failed", False),
        ("setup-failed", False),
        ("startup-failed", False),
        ("xfail", False),
        ("passed", True),
    ],
)
def test_lab_fixture_retains_failure_evidence_without_moving_working_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outcome: str, keep: bool
) -> None:
    root = tmp_path / "caller-root"
    root.mkdir()
    (root / "working-file").write_text("retained locally")
    evidence = tmp_path / "pytest-base" / "test-case"
    lab = Lab.__new__(Lab)
    lab._stop_started = False
    lab.root = root
    lab.events = EventLog()
    lab.observer = None
    if outcome == "startup-failed":
        lab._start = Mock(side_effect=RuntimeError("synthetic startup failure"))
        lab._port_reservations = Mock()
        lab._clients = []
        lab.model = None
        lab._processes = []
        lab._kill_stray_host_processes = Mock()
        lab._proxies = None
        lab._loop = Mock()
        lab.start = Mock(wraps=lab.start)
        lab.stop = Mock(wraps=lab.stop)
    else:
        lab.start = Mock(return_value=lab)
        lab.stop = Mock()
    lab.remove = Mock(wraps=lab.remove)
    lab.describe = Mock(return_value="synthetic lab")
    lab.write_diagnostics = Mock(wraps=lab.write_diagnostics)
    order = Mock()
    order.attach_mock(lab.write_diagnostics, "retain")
    order.attach_mock(lab.stop, "stop")
    constructor = Mock(return_value=lab)
    monkeypatch.setattr(resilience_fixtures, "Lab", constructor)
    monkeypatch.setattr(resilience_fixtures, "require_tmux", lambda: None)
    monkeypatch.setenv(resilience_fixtures.KEEP_ENV, "1" if keep else "0")
    report = SimpleNamespace(failed=outcome in {"failed", "setup-failed", "startup-failed"})
    if outcome == "xfail":
        report.wasxfail = "synthetic expected failure"
    request = SimpleNamespace(node=SimpleNamespace(_resilience_call=report))
    if outcome in {"setup-failed", "startup-failed"}:
        request.node = SimpleNamespace(_resilience_setup=report)
    fixture = resilience_fixtures.lab_factory.__wrapped__(request, evidence)
    start = next(fixture)
    if outcome == "startup-failed":
        with pytest.raises(RuntimeError, match="synthetic startup failure"):
            start(root=root)
    else:
        assert start(root=root) is lab
    assert constructor.call_args.args[0].root == root
    with pytest.raises(StopIteration):
        next(fixture)

    failed = outcome != "passed"
    assert lab.root == root
    if outcome == "startup-failed":
        assert lab.stop.call_count == 2
        assert lab._port_reservations.close.call_count == lab._loop.stop.call_count == 2
        assert lab._kill_stray_host_processes.call_count == 2
    else:
        lab.stop.assert_called_once_with()
    assert lab.remove.call_count == (0 if failed or keep else 1)
    assert (root / "working-file").exists() == (failed or keep)
    assert root.exists() == (failed or keep)
    assert (evidence / "resilience-diagnostics" / "lab-0.json").exists() == failed
    if failed:
        lab.write_diagnostics.assert_called_once_with(
            evidence / "resilience-diagnostics" / "lab-0.json"
        )
        assert [call[0] for call in order.mock_calls] == (
            ["stop", "retain", "stop"] if outcome == "startup-failed" else ["retain", "stop"]
        )
        payload = json.loads((evidence / "resilience-diagnostics" / "lab-0.json").read_text())
        assert payload["captured_before_stop"] is (outcome != "startup-failed")
    else:
        lab.write_diagnostics.assert_not_called()


@pytest.mark.parametrize("snapshot_budget", [False, True], ids=["export-error", "snapshot-budget"])
def test_lab_diagnostic_failure_still_stops_all_labs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot_budget: bool,
) -> None:
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(
        lab_module, "time", SimpleNamespace(time=lambda: 123.0, monotonic=lambda: clock.now)
    )
    timeouts = []
    stop_times = []

    def respond(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        clock.now += 0.75 if len(timeouts) % 2 else 1.25
        if len(timeouts) % 2 == 0:
            raise httpx.ReadTimeout("private-diagnostic-error", request=request)
        return httpx.Response(200, json={"status": "idle"})

    labs = []
    with httpx.Client(
        base_url="http://example.test", transport=httpx.MockTransport(respond)
    ) as observer:
        for index in range(2):
            lab = Lab.__new__(Lab)
            lab._stop_started = False
            lab.root = tmp_path / f"working-{index}"
            lab.events = EventLog()
            lab.observer = observer
            lab.start = Mock(return_value=lab)
            lab.stop = Mock(side_effect=lambda: stop_times.append(clock.now))
            lab.remove = Mock()
            lab.describe = Mock(return_value="synthetic lab")
            for session in range(10):
                lab.events.emit("lab", "session_created", session_id=f"conv_{session:032x}")
            if not snapshot_budget:
                lab.write_diagnostics = Mock(side_effect=OSError("private-diagnostic-error"))
            labs.append(lab)
        monkeypatch.setattr(resilience_fixtures, "Lab", Mock(side_effect=labs))
        monkeypatch.setattr(resilience_fixtures, "require_tmux", lambda: None)
        request = SimpleNamespace(
            node=SimpleNamespace(_resilience_call=SimpleNamespace(failed=True))
        )
        fixture = resilience_fixtures.lab_factory.__wrapped__(request, tmp_path)
        start = next(fixture)
        start()
        start()
        with pytest.raises(StopIteration):
            next(fixture)
    for lab in labs:
        lab.stop.assert_called_once_with()
        lab.remove.assert_not_called()
    assert "private-diagnostic-error" not in capsys.readouterr().out
    if snapshot_budget:
        assert timeouts == [2.0, 1.25, 2.0, 1.25]
        assert stop_times == [2.0, 4.0]
        for index in range(2):
            raw = (tmp_path / "resilience-diagnostics" / f"lab-{index}.json").read_text()
            payload = json.loads(raw)
            assert [snapshot["read_status"] for snapshot in payload["sessions"]] == [
                "ok",
                "unavailable",
            ]
            assert payload["sessions_omitted"] == 8
            assert payload["snapshot_budget_exhausted"] is True
            assert "private-diagnostic-error" not in raw


@pytest.mark.parametrize(
    "invalid_tail,churn",
    [(False, "none"), (True, "none"), (False, "proxy"), (False, "discovery"), (False, "native")],
    ids=["valid-tail", "invalid-tail", "proxy-churn", "discovery-churn", "native-bounds"],
)
def test_lab_diagnostics_bound_aggregate_history_and_session_snapshots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, invalid_tail: bool, churn: str
) -> None:
    monkeypatch.setattr(
        lab_module, "time", SimpleNamespace(time=lambda: 123.0, monotonic=lambda: 0.0)
    )
    lab = Lab.__new__(Lab)
    lab._stop_started = False
    lab.root = tmp_path / "working"
    lab.events = EventLog()
    identities = [f"conv_{index:032x}" for index in range(10)]
    if churn == "discovery":
        for index in range(4097):
            lab.events.emit("lab", "session_created", session_id=f"conv_f{index:031x}")
    for identity in identities:
        lab.events.emit("lab", "session_created", session_id=identity)
    for index in range(3):
        lab.events.emit(
            "observer",
            "timeline",
            session_id=identities[index],
            observations=[
                {
                    "wall": wall,
                    "source": "snapshot",
                    "status": "private-invalid-status"
                    if invalid_tail and wall >= 3404
                    else "running",
                }
                for wall in range(index * 2500, (index + 1) * 2500)
            ],
        )
    if churn == "proxy":
        for index in range(4097):
            lab.events.emit("proxy:host", "connect", conn=index, tag="runner.tunnel")
    if churn == "native":
        bridge = lab.root / "t" / "omnigent-123" / "claude-native" / ("c" * 32)
        bridge.mkdir(parents=True)
        for index in range(130):
            launch_id = f"{index:032x}"
            (bridge / f"lifecycle-{launch_id}.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "terminal_instance_id": "d" * 32,
                        "launch_id": launch_id,
                        "events": [
                            {"event_name": "Stop", "recorded_at": hook} for hook in range(20)
                        ],
                    }
                )
            )
    project = Mock(wraps=lab_module._diagnostic_fields)
    monkeypatch.setattr(lab_module, "_diagnostic_fields", project)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json={"status": "idle"})

    destination = tmp_path / "resilience-diagnostics" / "lab-0.json"
    with httpx.Client(
        base_url="http://example.test", transport=httpx.MockTransport(respond)
    ) as observer:
        lab.observer = observer
        lab.write_diagnostics(destination)
    raw = destination.read_text()
    payload = json.loads(raw)
    if churn == "proxy":
        assert len(payload["events"]) == 4096
        assert [event["conn"] for event in payload["events"]] == list(range(1, 4097))
    elif churn == "discovery":
        assert len(payload["events"]) == 4093
        assert [event["session_id"] for event in payload["events"][-10:]] == identities
    else:
        assert [event["session_id"] for event in payload["events"]] == identities
    assert [observation["wall"] for observation in payload["observations"]] == (
        [] if invalid_tail else list(range(3404, 7500))
    )
    assert sum("wall" in call.args[0] for call in project.call_args_list) == 4096
    assert payload["observations_omitted"] == 3404
    assert [snapshot["session_id"] for snapshot in payload["sessions"]] == identities[:8]
    assert requests == [f"/v1/sessions/{identity}" for identity in identities[:8]]
    assert payload["sessions_omitted"] == (4085 if churn == "discovery" else 2)
    assert payload["discovery_events_omitted"] == (14 if churn == "discovery" else 0)
    assert payload["snapshot_budget_exhausted"] is False
    assert "private-invalid-status" not in raw
    if churn == "native":
        native = payload["native_lifecycle"]
        assert native["candidates"] == 130
        assert native["omitted"] == 2
        assert native["rejected"] == 0
        assert len(native["records"]) == 128
        assert [record["launch_id"] for record in native["records"]] == [
            f"{index:032x}" for index in range(128)
        ]
        for record in native["records"]:
            assert record["events"] == [
                {"event_name": "Stop", "recorded_at": hook} for hook in range(4, 20)
            ]


@pytest.mark.parametrize("snapshot_available", [True, False])
def test_lab_diagnostics_project_records_and_upload_only_safe_file(
    tmp_path: Path, snapshot_available: bool
) -> None:
    root = tmp_path / "lab-working-root"
    root.mkdir()
    session_id = "conv_" + "a" * 32
    launch_id = "b" * 32
    private = "private-content-must-stay-local"
    lab = Lab.__new__(Lab)
    lab._stop_started = False
    lab.root = root
    lab.config = LabConfig(root=root)
    lab.events = EventLog()
    lab.events.emit(
        "lab",
        "session_created",
        session_id=session_id,
        harness="claude",
        env=private,
        config=private,
        keys=private,
        credentials=private,
        database=private,
        raw_model_payload=private,
    )
    lab.events.emit("lab", "process_start", process="runner", pid=123, argv=private)
    lab.events.emit("proxy:host", "connect", conn=4, tag="runner.tunnel", request=private)
    lab.events.emit(private, private, headers=private)
    lab.events.emit(
        "observer",
        "timeline",
        session_id=session_id,
        observations=[
            {"wall": 10.0, "source": "stream", "status": "idle", "headers": private},
            {"wall": 11.0, "source": "stream", "status": "running"},
            {"wall": 12.0, "source": "snapshot", "status": "running", "runner_online": True},
            {"wall": float("nan"), "source": private, "status": private},
        ],
    )
    bridge = root / "t" / "omnigent-123" / "claude-native" / ("c" * 32)
    bridge.mkdir(parents=True)
    lifecycle = {
        "schema_version": 1,
        "terminal_instance_id": "d" * 32,
        "launch_id": launch_id,
        "launch_session_id": session_id,
        "events": [
            {
                "event_name": "Stop",
                "recorded_at": 11.5,
                "claude_session_id": "e" * 32,
                "bridge_session_id": session_id,
                "payload": private,
                "source": private,
            }
        ],
        "headers": private,
    }
    (bridge / f"lifecycle-{launch_id}.json").write_text(json.dumps(lifecycle))
    for path in (
        root / "config.yaml",
        root / "chat.db",
        bridge / "bridge.json",
        root / "events.jsonl",
    ):
        path.write_text(private)
    (root / "logs").mkdir()
    (root / "logs" / "runner.log").write_text(private)

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{session_id}"
        assert request.extensions["timeout"]["read"] <= 2
        if not snapshot_available:
            raise RuntimeError(private)
        return httpx.Response(
            200,
            json={
                "status": "running",
                "runner_online": True,
                "metadata": private,
                "labels": {"token": private},
            },
        )

    evidence = tmp_path / "pytest-base" / "test-case" / "resilience-diagnostics"
    destination = evidence / "lab-0.json"
    with httpx.Client(
        base_url="http://example.test", transport=httpx.MockTransport(respond)
    ) as observer:
        lab.observer = observer
        lab.write_diagnostics(destination)
    raw = destination.read_text()
    payload = json.loads(raw)
    assert private not in raw
    assert str(root) not in raw
    expected_snapshot = {"session_id": session_id, "read_status": "unavailable"}
    if snapshot_available:
        expected_snapshot.update(read_status="ok", status="running", runner_online=True)
    assert payload["sessions"] == [expected_snapshot]
    assert [item["status"] for item in payload["observations"]] == ["idle", "running", "running"]
    assert [item["wall"] for item in payload["observations"]] == [10.0, 11.0, 12.0]
    assert payload["events"][1]["pid"] == 123
    assert payload["events"][2]["conn"] == 4
    assert payload["native_lifecycle"]["candidates"] == 1
    assert payload["native_lifecycle"]["rejected"] == payload["native_lifecycle"]["omitted"] == 0
    assert payload["native_lifecycle"]["records"][0]["terminal_instance_id"] == "d" * 32
    assert payload["native_lifecycle"]["records"][0]["events"] == [
        {
            "event_name": "Stop",
            "recorded_at": 11.5,
            "claude_session_id": "e" * 32,
            "bridge_session_id": session_id,
        }
    ]
    assert (root / "config.yaml").read_text() == private

    for name in ("config.yaml", "private.json", "runner.log", "lab-0.db"):
        (evidence / name).write_text(private)
    action = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/actions/e2e-run/action.yml").read_text()
    )
    upload = next(
        step
        for step in action["runs"]["steps"]
        if step.get("name") == "Upload server logs on failure"
    )
    selectors = [
        line for line in upload["with"]["path"].splitlines() if "resilience-diagnostics" in line
    ]
    selected = {
        path
        for selector in selectors
        for path in (tmp_path / "pytest-base").glob("**/" + selector.split("/**/", 1)[1])
    }
    assert selected == {destination}


@pytest.mark.parametrize(
    "invalid",
    [
        "file-link",
        "directory-link",
        "oversize",
        "malformed",
        "nested",
        "events-object",
        "identity-mismatch",
    ],
)
def test_lab_diagnostics_reject_outside_or_invalid_native_records(
    tmp_path: Path, invalid: str
) -> None:
    lab = Lab.__new__(Lab)
    lab._stop_started = False
    lab.root = tmp_path / "working"
    lab.config = LabConfig(root=lab.root)
    lab.events = EventLog()
    lab.events.emit("lab", "process_start", process="runner", pid=123)
    lab.observer = None
    bridge = lab.root / "t" / "omnigent-123" / "claude-native" / ("c" * 32)
    bridge.mkdir(parents=True)
    launch_id = "b" * 32
    path = bridge / f"lifecycle-{launch_id}.json"
    value = {
        "schema_version": 1,
        "terminal_instance_id": "d" * 32,
        "launch_id": launch_id,
        "events": [{"event_name": "Stop", "recorded_at": 1.0}],
    }
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / path.name
    source.write_text(json.dumps(value))
    if invalid == "file-link":
        path.symlink_to(source)
    elif invalid == "directory-link":
        bridge.rmdir()
        bridge.symlink_to(outside, target_is_directory=True)
    elif invalid == "oversize":
        path.write_text(json.dumps({**value, "payload": "x" * 32768}))
    elif invalid == "malformed":
        path.write_text("private-invalid-json")
    elif invalid == "nested":
        path.write_text('{"payload":' + "[" * 16000 + "0" + "]" * 16000 + "}")
    elif invalid == "events-object":
        path.write_text(json.dumps({**value, "events": {}}))
    else:
        path.write_text(json.dumps({**value, "launch_id": "a" * 32}))
    destination = tmp_path / "pytest-base" / "resilience-diagnostics" / "lab-0.json"
    lab.write_diagnostics(destination)
    payload = json.loads(destination.read_text())
    assert payload["events"][0]["pid"] == 123
    native = payload["native_lifecycle"]
    assert native["records"] == []
    assert native["rejected"] == native["candidates"]
    assert "private-invalid-json" not in destination.read_text()
    assert source.read_text() == json.dumps(value)


def test_scenario_retains_uncompressed_observation_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watcher = SessionWatcher("http://example.test", "conv_" + "a" * 32)
    watcher.observations = [
        Observation(wall=10.0, source="snapshot", status="running"),
        Observation(wall=11.0, source="snapshot", status="running"),
    ]
    monkeypatch.setattr(SessionWatcher, "start", lambda self: self)
    monkeypatch.setattr(SessionWatcher, "stop", lambda self: None)
    monkeypatch.setattr(_contract, "SessionWatcher", lambda *_: watcher)
    monkeypatch.setattr(_contract, "_start_recorder", lambda *_: None)
    lab = SimpleNamespace(
        server_url="http://example.test", root=Path("synthetic-lab"), events=EventLog()
    )
    with _contract.observe(lab, SimpleNamespace(session_id=watcher.session_id), Mock()):
        pass
    timeline = lab.events.events(source="observer", kind="timeline")
    assert len(timeline) == 1
    assert [entry["wall"] for entry in timeline[0].fields["observations"]] == [10.0, 11.0]
