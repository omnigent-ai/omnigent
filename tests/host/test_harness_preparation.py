"""Managed-sandbox harness auto-preparation: the ``IS_SANDBOX`` gate, the
install-only-when-absent decision, and the non-blocking start. Most cases run the
prepare thread inline so dispatch is deterministic; the npm install is patched out."""

from __future__ import annotations

import threading
import types
from collections.abc import Callable, Sequence

import pytest

from omnigent.host import harness_preparation as hp
from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.harness_install import PI_KEY, HarnessInstallResult


class _SyncThread:
    """A ``threading.Thread`` stand-in that runs its target inline on start."""

    def __init__(
        self,
        *,
        target: Callable[..., object],
        args: Sequence[object] = (),
        **_thread_kwargs: object,
    ) -> None:
        self._target = target
        self._args = tuple(args)

    def start(self) -> None:
        self._target(*self._args)


@pytest.fixture
def inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hp, "threading", types.SimpleNamespace(Thread=_SyncThread))


def _record_installs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch the binary lookup (absent) + installer; return the install attempts."""
    attempts: list[str] = []

    def fake_try_install(key: str) -> HarnessInstallResult:
        attempts.append(key)
        return HarnessInstallResult(True, None)

    monkeypatch.setattr(hp, "harness_cli_binary", lambda key: None)
    monkeypatch.setattr(hp, "try_install_harness_cli", fake_try_install)
    return attempts


@pytest.mark.usefixtures("inline_threads")
def test_installs_missing_pi_on_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    attempts = _record_installs(monkeypatch)
    assert hp.prepare_host_harnesses() is True
    assert attempts == [PI_KEY]


@pytest.mark.usefixtures("inline_threads")
@pytest.mark.parametrize("probe", ["version too low", "probe failed"])
def test_present_pi_is_never_upgraded(monkeypatch: pytest.MonkeyPatch, probe: str) -> None:
    """A resolvable ``pi`` is left alone even when its version check would fail:
    preparation is presence-only and readiness owns the version verdict."""
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.delenv("OMNIGENT_PI_PATH", raising=False)
    monkeypatch.setattr(hi, "resolve_cli_binary", lambda _name: "/opt/pi/bin/pi")

    probes: list[tuple[object, ...]] = []

    def version_probe(*args: object, **_k: object) -> bool:
        probes.append(args)
        if probe == "probe failed":
            raise RuntimeError("pi --version exploded")
        return False

    monkeypatch.setattr(hi, "_harness_cli_version_satisfies", version_probe)
    attempts: list[str] = []
    monkeypatch.setattr(hp, "try_install_harness_cli", lambda key: attempts.append(key))
    assert hp.prepare_host_harnesses() is True
    assert attempts == []
    # Presence alone decides here; the version probe is never consulted.
    assert probes == []


@pytest.mark.usefixtures("inline_threads")
@pytest.mark.parametrize("marker", [None, "", " ", "0"])
def test_non_sandbox_marker_is_a_noop(monkeypatch: pytest.MonkeyPatch, marker: str | None) -> None:
    if marker is None:
        monkeypatch.delenv("IS_SANDBOX", raising=False)
    else:
        monkeypatch.setenv("IS_SANDBOX", marker)
    attempts = _record_installs(monkeypatch)
    assert hp.prepare_host_harnesses() is False
    assert attempts == []


@pytest.mark.usefixtures("inline_threads")
@pytest.mark.parametrize("failing", ["harness_cli_binary", "try_install_harness_cli"])
def test_install_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch, failing: str) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")

    def boom(key: str, *_a: object, **_k: object) -> object:
        raise RuntimeError(f"{failing} exploded")

    monkeypatch.setattr(hp, "harness_cli_binary", lambda key: None)
    monkeypatch.setattr(hp, failing, boom)
    # Best-effort: a failing lookup or installer must leave the daemon starting
    # normally, not crash host boot.
    assert hp.prepare_host_harnesses() is True


def test_worker_start_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")

    class _UnstartableThread(_SyncThread):
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(hp, "threading", types.SimpleNamespace(Thread=_UnstartableThread))
    attempts = _record_installs(monkeypatch)
    # Thread exhaustion is logged; host boot continues without preparation.
    assert hp.prepare_host_harnesses() is False
    assert attempts == []


@pytest.mark.usefixtures("inline_threads")
def test_spec_lookup_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    attempts = _record_installs(monkeypatch)

    def broken_spec(key: str) -> object:
        raise RuntimeError("plugin install spec exploded")

    monkeypatch.setattr(hp, "harness_install_spec", broken_spec)
    assert hp.prepare_host_harnesses() is False
    assert attempts == []


def test_preparation_returns_before_the_install_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boot hook must not wait for npm: the install runs on its own thread."""
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setattr(hp, "harness_cli_binary", lambda key: None)
    started = threading.Event()
    release = threading.Event()

    def blocked_install(key: str) -> HarnessInstallResult:
        started.set()
        assert release.wait(timeout=10)
        return HarnessInstallResult(True, None)

    monkeypatch.setattr(hp, "try_install_harness_cli", blocked_install)
    worker_name = f"host-harness-prepare-{PI_KEY}"
    try:
        assert hp.prepare_host_harnesses() is True
        assert started.wait(timeout=10)
        workers = [thread for thread in threading.enumerate() if thread.name == worker_name]
        assert len(workers) == 1 and workers[0].is_alive() and workers[0].daemon
    finally:
        release.set()
        for thread in threading.enumerate():
            if thread.name == worker_name:
                thread.join(timeout=10)
