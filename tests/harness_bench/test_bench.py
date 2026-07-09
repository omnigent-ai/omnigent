"""Bench conformance tests.

Two layers, matching the design doc:

- **Offline** (always, no network/creds): registry membership, profile
  completeness, reconciliation semantics, community-profile resolution,
  and that the matrix renders. Fast enough for every PR.
- **Live** (gated on ``--profile`` + a runnable harness CLI): runs the
  full probe set against each official harness and asserts P0 dimensions
  match what the profile declares — i.e. no ``DRIFT`` and a working
  ``basic_turn``.
"""

from __future__ import annotations

import json

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from tests.harness_bench.bench import run_bench, run_harness
from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.manifest import OFFICIAL_PROFILES
from tests.harness_bench.probes import ALL_PROBES
from tests.harness_bench.profile import BenchProfile, resolve_profile
from tests.harness_bench.report import render_json, render_markdown
from tests.harness_bench.verdict import Priority, Verdict, reconcile

_OFFICIAL = list(OFFICIAL_PROFILES.values())
_OFFICIAL_IDS = [p.harness for p in _OFFICIAL]

# A community-style profile used to prove name-based resolution of an
# out-of-repo harness that ships its own BenchProfile.
_FAKE_PROFILE = BenchProfile(
    harness="fake-community",
    model="databricks-claude-sonnet-4-6",
    env_prefix="HARNESS_FAKE_",
    marker="FAKE_OK",
)


# ── Offline layer ───────────────────────────────────────────────


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_official_harness_registered(profile: BenchProfile) -> None:
    assert profile.harness in _HARNESS_MODULES, (
        f"{profile.harness!r} has a bench profile but is not in _HARNESS_MODULES"
    )


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_profile_fields_wellformed(profile: BenchProfile) -> None:
    assert profile.model, "profile must declare a test model"
    assert profile.env_prefix.endswith("_"), "env_prefix must end with '_'"
    assert profile.marker, "profile must declare a marker"


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
def test_declared_covers_every_p0_dimension(profile: BenchProfile) -> None:
    # Every P0 probe must have a declared verdict, or drift can never fire
    # for that cell — the bench would silently under-report a regression.
    for probe in ALL_PROBES:
        if probe.priority is Priority.P0:
            assert profile.declared_for(probe.name) is not Verdict.UNKNOWN, (
                f"{profile.harness!r} declares no verdict for P0 dimension {probe.name!r}"
            )


def test_streaming_capability_declares_binary_verdict() -> None:
    # Guards the kiro-native drift: streaming declares binary (True→SUPPORTED,
    # False→UNSUPPORTED), never PARTIAL.
    from omnigent.harness_plugins import harness_capabilities
    from tests.harness_bench.manifest import _declared_from_capabilities

    caps = harness_capabilities()
    for harness, cap in caps.items():
        declared = _declared_from_capabilities(harness).get("streaming")
        if declared is None:
            continue
        expected = Verdict.SUPPORTED if cap.streaming else Verdict.UNSUPPORTED
        assert declared is expected, (
            f"{harness!r}: streaming={cap.streaming} should declare {expected.name}, "
            f"got {declared.name}"
        )
        assert declared is not Verdict.PARTIAL, f"{harness!r}: PARTIAL is never a declared verdict"


def test_reconcile_flags_concrete_mismatch() -> None:
    assert reconcile(Verdict.UNSUPPORTED, Verdict.SUPPORTED) is Verdict.DRIFT
    assert reconcile(Verdict.SUPPORTED, Verdict.UNSUPPORTED) is Verdict.DRIFT
    assert reconcile(Verdict.PARTIAL, Verdict.SUPPORTED) is Verdict.DRIFT


def test_reconcile_silent_when_either_side_inconclusive() -> None:
    assert reconcile(Verdict.SUPPORTED, Verdict.SUPPORTED) is Verdict.SUPPORTED
    assert reconcile(Verdict.SKIPPED, Verdict.SUPPORTED) is Verdict.SKIPPED
    assert reconcile(Verdict.SUPPORTED, Verdict.UNKNOWN) is Verdict.SUPPORTED


def test_resolve_official_and_community_and_unknown() -> None:
    assert resolve_profile("codex").harness == "codex"
    assert resolve_profile("tests.harness_bench.test_bench:_FAKE_PROFILE") is _FAKE_PROFILE
    with pytest.raises(KeyError):
        resolve_profile("no-such-harness")


def test_resolve_registered_harness_by_name() -> None:
    """A registered harness with no official profile is resolvable by name.

    This is the "plugs in with no bench edit" path: an in-repo ACP/CLI-subprocess
    harness (not auto-derived, since that is native-tui-only) resolves via the
    registry fallback, deriving a profile from the capability model. ACP is an
    ACP_SUBPROCESS harness, so it lands on the SDK-wrap driver family
    (transport "sdk-inproc"), not native-tui.
    """
    from omnigent.harness_plugins import harness_modules

    if "acp" not in harness_modules():
        pytest.skip("acp harness not registered in this build")
    profile = resolve_profile("acp")
    assert profile.harness == "acp"
    assert profile.transport == "sdk-inproc"

    # acp:<slug> is a first-class id: base `acp` is registered and the slug
    # selects a configured ACP agent at spawn. It binds, keeping the full id as
    # the harness (so config.harness=acp:qwen reaches the runner) and a valid
    # env-prefix stem. An empty slug ("acp:") is refused.
    slug = resolve_profile("acp:qwen")
    assert slug.harness == "acp:qwen"
    assert slug.transport == "sdk-inproc"
    assert slug.env_prefix == "HARNESS_ACP_QWEN_"
    with pytest.raises(KeyError):
        resolve_profile("acp:")


def test_resolve_entry_point_plugin_and_alias() -> None:
    """An entry-point community plugin resolves by name AND by alias.

    ``omnigent-rovo`` registers ``rovo-cli`` (alias ``rovo``) via the
    ``omnigent.community.harness`` entry point and declares no capabilities
    entry — only a harness module + install spec. The registry fallback still
    binds it (keying off harness_modules, defaulting to the SDK family) and
    skip-gates on its install-spec binary. Gated on the plugin being installed
    so a build without it still passes.
    """
    from omnigent.harness_plugins import harness_aliases

    if harness_aliases().get("rovo") != "rovo-cli":
        pytest.skip("omnigent-rovo plugin not installed")
    by_alias = resolve_profile("rovo")
    by_name = resolve_profile("rovo-cli")
    assert by_alias.harness == "rovo-cli" == by_name.harness
    assert by_alias.transport == "sdk-inproc"
    assert by_alias.cli_binary == "acli"  # skip-gates on the Atlassian CLI
    # A model is always stamped (agent registration requires a non-empty one);
    # it is inert for an own-auth harness, which uses its own.
    assert by_alias.model


def test_registry_profile_happy_path_no_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry fallback's positive path, independent of any optional plugin.

    Fakes a registered CLI-subprocess harness (+ alias + install-spec binary) so
    the name/alias resolution, the integration_mode -> sdk-inproc mapping, and
    the install-spec skip-gate are exercised even in a build without
    omnigent-rovo. Guards the coverage the plugin tests skip-gate away.
    """
    from types import SimpleNamespace

    import tests.harness_bench.manifest as man
    from omnigent.harness_capabilities import AuthModel, IntegrationMode

    class _Spec:
        binary = "fakebin"

    # A lightweight caps stand-in: _registry_profile + the prose/declared helpers
    # only read integration_mode / auth / streaming / interrupt.
    caps = SimpleNamespace(
        integration_mode=IntegrationMode.CLI_SUBPROCESS,
        auth=AuthModel.OWN_AUTH,
        streaming=True,
        interrupt=True,
    )
    monkeypatch.setattr(man, "harness_modules", lambda: {"fake-cli": "pkg.fake"})
    monkeypatch.setattr(man, "harness_aliases", lambda: {"fake": "fake-cli"})
    monkeypatch.setattr(man, "harness_capabilities", lambda: {"fake-cli": caps})
    monkeypatch.setattr(man, "harness_install_keys", lambda: {"fake-cli": "fake"})
    monkeypatch.setattr(man, "install_specs", lambda: {"fake": _Spec()})

    for name in ("fake-cli", "fake"):
        p = man._registry_profile(name)
        assert p is not None and p.harness == "fake-cli"
        assert p.transport == "sdk-inproc"
        assert p.cli_binary == "fakebin"
        assert p.model  # always non-empty (agent registration requires a model)


def test_registry_refuses_native_server_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MODELED mode the bench has no driver for is refused, not mis-bound.

    NATIVE_SERVER (e.g. opencode-native) has no bench driver. The fallback must
    return None (-> resolve_profile KeyError) rather than silently degrade to
    the sdk-inproc default, which would bind a vendor-server harness to the SDK
    drivers and drop its skip-gate.
    """
    from types import SimpleNamespace

    import tests.harness_bench.manifest as man
    from omnigent.harness_capabilities import AuthModel, IntegrationMode

    caps = SimpleNamespace(
        integration_mode=IntegrationMode.NATIVE_SERVER,
        auth=AuthModel.OWN_AUTH,
        streaming=False,
        interrupt=False,
    )
    monkeypatch.setattr(man, "harness_modules", lambda: {"srv": "pkg.srv"})
    monkeypatch.setattr(man, "harness_aliases", dict)
    monkeypatch.setattr(man, "harness_capabilities", lambda: {"srv": caps})
    assert man._registry_profile("srv") is None


def test_infra_failure_reason_classifies_auth_and_ignores_capability_gaps() -> None:
    from tests.harness_bench.driver import TurnResult, infra_failure_reason

    # A 403 gateway error is an environment problem -> yields a skip reason.
    auth = TurnResult(
        failed=True,
        error={
            "code": "RuntimeError",
            "message": "unexpected status 403 Forbidden: Invalid Token",
        },
    )
    reason = infra_failure_reason(auth)
    assert reason is not None
    assert "403" in reason

    # A plain failure with no infra marker is a real capability gap -> None.
    assert infra_failure_reason(TurnResult(failed=True, error="model refused the tool")) is None
    # A successful turn is never an infra failure.
    assert infra_failure_reason(TurnResult(completed=True, text="ok")) is None

    # Token-provisioning failures on full-server (codex/pi) are env/auth gaps,
    # not capability gaps -> must yield a skip reason, never a false UNSUPPORTED
    # that drifts against a SUPPORTED declaration.
    for msg in (
        "inner executor error: provider auth command `sh` produced an empty token",
        "PiExecutor(gateway=True) could not fetch a gateway token for the workspace host.",
        "Failed to resolve external API key auth",
    ):
        result = TurnResult(failed=True, error={"message": msg})
        assert infra_failure_reason(result) is not None, msg


async def test_offline_render_produces_matrix() -> None:
    matrix = await run_bench(_OFFICIAL, live=False)
    # Offline: nothing observed, so no drift and every cell is SKIPPED.
    assert not matrix.has_drift
    assert all(
        cell.observed is Verdict.SKIPPED for report in matrix.reports for cell in report.cells
    )
    md = render_markdown(matrix)
    assert "Harness capability matrix" in md
    for profile in _OFFICIAL:
        assert profile.harness in md
    # The harness column is labelled with the *resolved* transport: an SDK
    # harness shows its full-server default (not the sdk-inproc family marker),
    # a native shows the short `native` label.
    assert "`claude-sdk [full-server]`" in md
    assert "`claude-native [native]`" in md
    # JSON is well-formed and carries every harness, plus the resolved transport.
    payload = json.loads(render_json(matrix))
    assert {h["harness"] for h in payload["harnesses"]} == {p.harness for p in _OFFICIAL}
    by_harness = {h["harness"]: h for h in payload["harnesses"]}
    assert by_harness["claude-sdk"]["resolved_transport"] == "full-server"
    assert by_harness["claude-native"]["resolved_transport"] == "native-tui"


def test_grid_already_shown_only_for_grid_drawing_sink() -> None:
    """_grid_already_shown is True only for a sink that painted the grid."""
    from tests.harness_bench.__main__ import _grid_already_shown
    from tests.harness_bench.events import LineSink

    assert _grid_already_shown(None) is False
    assert _grid_already_shown(LineSink(lambda _m: None)) is False

    class _GridSink:
        drew_grid = True

    assert _grid_already_shown(_GridSink()) is True


async def test_render_table_grid_false_drops_grid_keeps_footer() -> None:
    """grid=False omits the heading + glyph rows but keeps the legend/notes.

    This is what the CLI emits when the rich live table already painted the grid
    on the same terminal: the report should add the per-cell explanations, not
    reprint the grid.
    """
    from tests.harness_bench.report import render_table

    matrix = await run_bench(_OFFICIAL, live=False)
    full = render_table(matrix, declared=True, grid=True)
    footer = render_table(matrix, declared=True, grid=False)

    # The full render has the heading + a harness row; the footer-only render
    # has neither, but both carry the legend.
    assert "Harness capability matrix" in full
    assert "claude-sdk" in full
    assert "Harness capability matrix" not in footer
    assert "claude-sdk" not in footer
    assert "Legend:" in footer
    # The offline note is suppressed, so a declared render's footer is just the
    # legend -- no dangling "Notes:" header.
    assert footer.strip().startswith("Legend:")


# ── Progress events / rich / parallel / report (offline) ─────────


async def test_run_harness_emits_structured_events_and_linesink_adapts() -> None:
    """run_harness emits typed events; a bare-callable progress adapts to LineSink.

    Uses a fake driver so no creds/subprocess are needed: a basic turn passes,
    which lets every probe run and produce a ProbeFinished.
    """
    from tests.harness_bench.driver import TurnResult
    from tests.harness_bench.events import (
        HarnessFinished,
        HarnessStarted,
        ProbeFinished,
        ProbeStarted,
        ProgressSink,
    )

    class _CaptureSink:
        def __init__(self) -> None:
            self.events: list = []

        def emit(self, event) -> None:
            self.events.append(event)

        def close(self) -> None:
            pass

    assert isinstance(_CaptureSink(), ProgressSink)  # structural conformance

    class _OKDriver:
        transport = "sdk-inproc"

        def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
            pass

        @staticmethod
        def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def run_basic_turn(self, marker: str) -> TurnResult:
            return TurnResult(completed=True, text=marker)

        async def run_streaming_turn(self) -> TurnResult:
            return TurnResult(completed=True, text_delta_count=5)

        async def run_tool_turn(self, *, deny: bool) -> TurnResult:
            return TurnResult(completed=True)

        async def run_interrupt_turn(self) -> TurnResult:
            return TurnResult(cancelled=True)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _OKDriver,
    )
    try:
        profile = BenchProfile(
            harness="fake-sdk", model="m", env_prefix="HARNESS_FAKE_SDK_", marker="FAKE_OK"
        )
        sink = _CaptureSink()
        await run_harness(profile, databricks_profile="oss", live=True, progress=sink)
    finally:
        monkeypatch.undo()

    kinds = [type(e).__name__ for e in sink.events]
    assert kinds[0] == "HarnessStarted"
    assert isinstance(sink.events[0], HarnessStarted)
    assert kinds[-1] == "HarnessFinished"
    assert isinstance(sink.events[-1], HarnessFinished)
    # Every probe that ran emits a started+finished pair.
    assert any(isinstance(e, ProbeStarted) for e in sink.events)
    finished = [e for e in sink.events if isinstance(e, ProbeFinished)]
    assert {e.probe for e in finished} >= {"basic_turn", "streaming"}

    # A bare callable is adapted to a LineSink (structured events → lines).
    lines: list[str] = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _OKDriver,
    )
    try:
        await run_harness(profile, databricks_profile="oss", live=True, progress=lines.append)
    finally:
        monkeypatch.undo()
    assert any("Basic turn" in ln for ln in lines)


async def test_run_bench_jobs_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """--jobs > 1 runs harnesses concurrently but keeps report order == input order."""
    import asyncio as _asyncio

    from tests.harness_bench.driver import TurnResult

    class _SlowDriver:
        transport = "sdk-inproc"

        def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
            self._h = profile.harness

        @staticmethod
        def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
            return None

        async def __aenter__(self):
            # Reverse-stagger the delay so, without order preservation, finish
            # order would differ from input order.
            await _asyncio.sleep(0.02 if self._h.endswith("1") else 0.01)
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def run_basic_turn(self, marker: str) -> TurnResult:
            return TurnResult(completed=True, text=marker)

        async def run_streaming_turn(self) -> TurnResult:
            return TurnResult(completed=True, text_delta_count=3)

        async def run_tool_turn(self, *, deny: bool) -> TurnResult:
            return TurnResult(completed=True)

        async def run_interrupt_turn(self) -> TurnResult:
            return TurnResult(cancelled=True)

    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _SlowDriver,
    )
    profiles = [
        BenchProfile(harness=f"fake-{i}", model="m", env_prefix=f"HARNESS_F{i}_", marker="X")
        for i in range(3)
    ]
    matrix = await run_bench(profiles, databricks_profile="oss", live=True, jobs=3)
    assert [r.profile.harness for r in matrix.reports] == ["fake-0", "fake-1", "fake-2"]


async def test_parallel_full_server_shares_one_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parallel full-server run builds ONE shared server, reused by every harness.

    Verifies the shared-server optimization: instead of N server+runner boots,
    one SharedFullServer is entered once and each harness registers its own
    agent+session on it.
    """
    from tests.harness_bench.driver import TurnResult

    built: list[object] = []

    class _FakeShared:
        def __init__(self, db_profile: str) -> None:
            built.append(self)
            self.registered: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def register_agent(self, profile, *, deny: bool) -> str:
            self.registered.append(profile.harness)
            return f"bench-{profile.harness}"

        def create_session(self, agent_name: str) -> str:
            return f"sess-{agent_name}"

    # A full-server driver that records which shared server it was handed.
    class _FSDriver:
        transport = "full-server"

        def __init__(self, profile, *, databricks_profile: str, shared=None) -> None:
            self._profile = profile
            self._shared = shared

        @staticmethod
        def unavailable(profile, *, databricks_profile):
            return None

        async def __aenter__(self):
            assert self._shared is not None  # parallel run injected the shared server
            self._shared.register_agent(self._profile, deny=False)
            self._shared.create_session(f"bench-{self._profile.harness}")
            return self

        async def __aexit__(self, *exc: object) -> None:
            pass

        async def run_basic_turn(self, marker: str) -> TurnResult:
            return TurnResult(completed=True, text=marker)

        async def run_streaming_turn(self) -> TurnResult:
            return TurnResult(completed=True, text_delta_count=3)

        async def run_tool_turn(self, *, deny: bool) -> TurnResult:
            return TurnResult(completed=True, tool_call_denied=deny)

        async def run_interrupt_turn(self) -> TurnResult:
            return TurnResult(cancelled=True)

    # bench imports SharedFullServer into its own namespace, so patch it there.
    monkeypatch.setattr("tests.harness_bench.bench.SharedFullServer", _FakeShared)
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _FSDriver,
    )

    profiles = [
        BenchProfile(
            harness=f"fs-{i}",
            model="m",
            env_prefix=f"HARNESS_FS{i}_",
            marker="X",
            transport="full-server",
        )
        for i in range(3)
    ]
    matrix = await run_bench(
        profiles, databricks_profile="oss", live=True, jobs=3, transport="full-server"
    )
    # Exactly one shared server, and all three harnesses registered on it.
    assert len(built) == 1
    assert sorted(built[0].registered) == ["fs-0", "fs-1", "fs-2"]
    assert [r.profile.harness for r in matrix.reports] == ["fs-0", "fs-1", "fs-2"]


def test_cli_writes_report_file(tmp_path) -> None:
    """`--report PATH` writes the matrix; format follows the extension."""
    from tests.harness_bench.__main__ import main

    md = tmp_path / "matrix.md"
    rc = main(["--no-live", "--report", str(md)])
    assert rc == 0
    text = md.read_text()
    assert "Harness capability matrix" in text and "| Harness |" in text

    js = tmp_path / "matrix.json"
    main(["--no-live", "--report", str(js)])
    payload = json.loads(js.read_text())
    assert payload.get("harnesses")


# ── Live layer (gated) ──────────────────────────────────────────


@pytest.fixture
def databricks_profile(request: pytest.FixtureRequest) -> str:
    profile = request.config.getoption("--profile")
    if not profile:
        pytest.skip("live bench requires --profile <name>")
    return str(profile)


@pytest.mark.parametrize("profile", _OFFICIAL, ids=_OFFICIAL_IDS)
async def test_live_harness_matches_declared(
    profile: BenchProfile, databricks_profile: str
) -> None:
    reason = SdkInprocDriver.unavailable(profile, databricks_profile=databricks_profile)
    if reason is not None:
        pytest.skip(f"{profile.harness}: {reason}")

    report = await run_harness(profile, databricks_profile=databricks_profile, live=True)

    basic = next(c for c in report.cells if c.probe_name == "basic_turn")
    if basic.observed is Verdict.SKIPPED:
        # Auth / gateway / connectivity problem (not a capability fact) —
        # the harness could not be exercised, so skip rather than fail.
        pytest.skip(f"{profile.harness}: {basic.note}")
    assert basic.observed is Verdict.SUPPORTED, (
        f"{profile.harness}: basic turn did not work ({basic.note}); "
        "the whole harness looks broken, not one capability"
    )
    drifted = [c for c in report.cells if c.is_drift]
    assert not drifted, (
        f"{profile.harness}: observed behavior drifted from the declared matrix: "
        + "; ".join(
            f"{c.title} declared {c.declared.name} but observed {c.observed.name} ({c.note})"
            for c in drifted
        )
    )


# ── full-server async shims (offline) ───────────────────────────


async def test_full_server_async_shims_delegate_to_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FullServerDriver async protocol methods delegate to the sync ones.

    The live gated tests exercise the sync entry points; this covers the
    asyncio.to_thread shims (and __aenter__/__aexit__) offline so a regression
    in the async binding is caught without a server+runner. Builds no driver
    state — every sync method is stubbed.
    """
    from tests.harness_bench.driver import TurnResult
    from tests.harness_bench.full_server_driver import FullServerDriver
    from tests.harness_bench.profile import BenchProfile

    profile = BenchProfile(harness="stub", model="m", env_prefix="HARNESS_STUB_", marker="STUB_OK")
    driver = FullServerDriver(profile, databricks_profile="oss")
    calls: list[str] = []

    def _stub(name: str, **kw: object):
        calls.append(f"{name}:{kw}")
        return TurnResult(completed=True)

    monkeypatch.setattr(driver, "__enter__", lambda: (calls.append("enter"), driver)[1])
    monkeypatch.setattr(driver, "__exit__", lambda *a: calls.append("exit"))
    monkeypatch.setattr(driver, "run_turn", lambda prompt, **kw: _stub("run_turn", prompt=prompt))
    monkeypatch.setattr(driver, "streaming_probe_turn", lambda **kw: _stub("streaming"))
    monkeypatch.setattr(driver, "tool_probe_turn", lambda **kw: _stub("tool", **kw))
    monkeypatch.setattr(driver, "interrupt_probe_turn", lambda **kw: _stub("interrupt"))

    async with driver as d:
        assert d is driver
        assert (await d.run_basic_turn("STUB_OK")).completed
        assert (await d.run_streaming_turn()).completed
        assert (await d.run_tool_turn(deny=True)).completed
        assert (await d.run_interrupt_turn()).completed

    assert calls[0] == "enter" and calls[-1] == "exit"
    assert any(c.startswith("tool:") and "True" in c for c in calls)


async def test_provisioning_failure_skips_and_tears_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A driver that raises in __aenter__ yields a skip AND is torn down.

    Provisioning spawns a server + daemon before the step that can fail (an
    own-auth native whose terminal never wires up), so the failure path must
    call __aexit__ or those subprocesses leak for the rest of a multi-harness
    run. Asserts both: the harness is a capability-neutral skip, and teardown ran.
    """
    torn_down: list[bool] = []

    class _FailingDriver:
        transport = "stub"

        def __init__(self, profile: BenchProfile, *, databricks_profile: str) -> None:
            pass

        @staticmethod
        def unavailable(profile: BenchProfile, *, databricks_profile: str | None) -> str | None:
            return None

        async def __aenter__(self):
            # Simulates _wire_native_forwarder raising after the server/daemon
            # are already up.
            raise RuntimeError("native forwarder did not wire up within 90.0s")

        async def __aexit__(self, *exc: object) -> None:
            torn_down.append(True)

    profile = BenchProfile(
        harness="stub-native", model="m", env_prefix="HARNESS_STUB_NATIVE_", marker="X"
    )
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _FailingDriver,
    )

    report = await run_harness(profile, databricks_profile="oss", live=True)

    assert report.skipped_reason is not None and "provisioning failed" in report.skipped_reason
    assert all(c.observed is Verdict.SKIPPED for c in report.cells)
    assert torn_down == [True], "provisioning-failure path must tear down the driver"


async def test_expected_provisioning_error_logged_quietly(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A ProvisioningError skips at INFO (no traceback); a generic error warns.

    The branch split keeps the matrix readable: a known-unrunnable environment
    (own-auth native not logged in) logs only its reason, while an unexpected
    exception keeps its full stack so a genuine driver bug can't hide behind a
    green-looking skip.
    """
    import logging

    from tests.harness_bench.driver import ProvisioningError

    def _driver_raising(exc: Exception):
        class _D:
            transport = "stub"

            def __init__(self, profile, *, databricks_profile: str) -> None:
                pass

            @staticmethod
            def unavailable(profile, *, databricks_profile):
                return None

            async def __aenter__(self):
                raise exc

            async def __aexit__(self, *e: object) -> None:
                pass

        return _D

    profile = BenchProfile(harness="stub", model="m", env_prefix="HARNESS_STUB_", marker="X")

    # Expected failure → a single INFO record, no exception/traceback attached.
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _driver_raising(
            ProvisioningError("cli not logged in")
        ),
    )
    with caplog.at_level(logging.INFO, logger="tests.harness_bench.bench"):
        await run_harness(profile, databricks_profile="oss", live=True)
    provisioning_logs = [r for r in caplog.records if "stub" in r.getMessage()]
    assert provisioning_logs, "expected a log line for the skip"
    assert all(r.levelno == logging.INFO and r.exc_info is None for r in provisioning_logs)

    # Unexpected failure → WARNING with the traceback attached.
    caplog.clear()
    monkeypatch.setattr(
        "tests.harness_bench.bench.resolve_driver_class",
        lambda p, *, override=None, fast=False: _driver_raising(RuntimeError("boom")),
    )
    with caplog.at_level(logging.INFO, logger="tests.harness_bench.bench"):
        await run_harness(profile, databricks_profile="oss", live=True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and any(r.exc_info is not None for r in warnings), (
        "an unexpected provisioning failure must keep its traceback"
    )


# ── native-tui transport (offline) ──────────────────────────────


def test_native_tui_registered_and_gates() -> None:
    """native-tui is in the registry and derives any native-tui harness."""
    from tests.harness_bench.native_tui_driver import NativeTuiDriver, native_vendor
    from tests.harness_bench.transport import driver_registry, resolve_driver_class

    assert driver_registry()["native-tui"] is NativeTuiDriver

    # A --transport override routes any profile to the native driver.
    claude_native = BenchProfile(
        harness="claude-native", model="m", env_prefix="HARNESS_CLAUDE_NATIVE_", marker="X"
    )
    assert resolve_driver_class(claude_native, override="native-tui") is NativeTuiDriver

    # Every native-tui harness derives a vendor from the capability model with
    # no per-vendor table — an own-auth native (cursor) as much as a shipped
    # credential one (claude). This is what lets a community-plugin native run
    # by name with no bench edit.
    assert native_vendor("claude-native") is not None
    cursor = native_vendor("cursor-native")
    assert cursor is not None and cursor.own_auth is True

    # A non-native-tui harness derives no vendor and gates cleanly: an SDK
    # harness, or a native-server one (opencode-native), is not this driver's.
    assert native_vendor("claude-sdk") is None
    codex_sdk = BenchProfile(harness="codex", model="m", env_prefix="X_", marker="X")
    assert NativeTuiDriver.unavailable(codex_sdk, databricks_profile="oss") is not None

    # No profile → the same capability-neutral skip contract as other drivers.
    assert NativeTuiDriver.unavailable(claude_native, databricks_profile=None) is not None


def test_transport_resolution_family_default_and_fast() -> None:
    """SDK family defaults to full-server; --fast downgrades it; natives unaffected.

    This is the core of the "full-server by default, --fast to opt out" model:
    the profile's transport is a family marker, and the effective driver comes
    from family + flags (see resolve_transport_name).
    """
    from tests.harness_bench.transport import resolve_transport_name

    sdk = BenchProfile(
        harness="codex", model="m", env_prefix="X_", marker="X", transport="sdk-inproc"
    )
    native = BenchProfile(
        harness="claude-native", model="m", env_prefix="X_", marker="X", transport="native-tui"
    )

    # SDK family: full-server by default, sdk-inproc under --fast.
    assert resolve_transport_name(sdk, override=None, fast=False) == "full-server"
    assert resolve_transport_name(sdk, override=None, fast=True) == "sdk-inproc"

    # native: a single transport --fast does not touch.
    assert resolve_transport_name(native, override=None, fast=False) == "native-tui"
    assert resolve_transport_name(native, override=None, fast=True) == "native-tui"

    # An explicit --transport wins over both the default and --fast, for any
    # family (the caller validates the name against the registry separately).
    assert resolve_transport_name(sdk, override="sdk-inproc", fast=False) == "sdk-inproc"
    assert resolve_transport_name(sdk, override="native-tui", fast=True) == "native-tui"


async def test_native_provisioning_http_error_becomes_provisioning_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP failure in native provisioning surfaces as a ProvisioningError.

    goose-native's terminal-ensure can 500 (the vendor cannot start a thread) —
    an environment/server-state gap, not a bench bug. __aenter__ must convert
    the raw httpx error into a ProvisioningError so run_harness logs it quietly
    (one INFO line) instead of dumping a traceback.
    """
    import httpx

    from tests.harness_bench.driver import ProvisioningError
    from tests.harness_bench.native_tui_driver import NativeTuiDriver

    profile = BenchProfile(
        harness="claude-native",
        model="m",
        env_prefix="HARNESS_CLAUDE_NATIVE_",
        marker="X",
        transport="native-tui",
    )
    driver = NativeTuiDriver(profile, databricks_profile="oss")

    def _boom() -> None:
        request = httpx.Request("POST", "http://localhost/resources/terminals")
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("500", request=request, response=response)

    monkeypatch.setattr(driver, "_provision", _boom)
    with pytest.raises(ProvisioningError) as exc_info:
        await driver.__aenter__()
    assert "500" in str(exc_info.value)


def test_full_server_skips_native_with_accurate_message() -> None:
    """full-server rejects a native profile by naming the native transport.

    A native harness forced onto full-server (via --transport) cannot run
    there (bundle registration, not host-daemon provisioning). The skip must
    name native-tui as the answer, not misreport the 'sdk-inproc' driver.
    """
    from tests.harness_bench.full_server_driver import FullServerDriver

    # Real native profiles carry transport="native-tui" (set in the manifest);
    # that is what the full-server gate keys on.
    claude_native = BenchProfile(
        harness="claude-native",
        model="m",
        env_prefix="HARNESS_CLAUDE_NATIVE_",
        marker="X",
        transport="native-tui",
    )
    reason = FullServerDriver.unavailable(claude_native, databricks_profile="oss")
    assert reason is not None
    assert "native-tui" in reason and "sdk-inproc" not in reason
