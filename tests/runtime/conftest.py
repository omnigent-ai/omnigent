"""
Ambient-discovery isolation for the runtime spawn-env tests.

The spawn-env builders merge ambient provider detection
(``omnigent/onboarding/ambient.py``) into the effective config, and several
of those probes read real host state that no amount of ``$HOME`` isolation
can reach: vendor API keys in the environment (bare and ``OMNIGENT_``-
prefixed, plus the Claude-on-Vertex GCP triple), a TCP connect to a local
Ollama server, and (on macOS) a ``claude auth status`` subprocess that reads
the Keychain. Any one synthesizes an ambient provider that shadows the
configured one under test, so the same test passes on a clean box and fails
on a developer's machine.

The autouse fixture here severs those host-state channels while keeping the
``$HOME``-scoped credentials-file check intact, so a test can still simulate
a login by writing that file under its isolated home. It also clears the
harness login-probe TTL cache around every test, since a positive verdict
cached by one test outlives that test by two minutes.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from omnigent.onboarding import ambient, harness_install
from omnigent.onboarding.providers import PROVIDER_ENV_VARS

# Claude Code's Vertex AI routing is detected from these three GCP env vars,
# which PROVIDER_ENV_VARS does not cover.
_VERTEX_ENV_VARS = (
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
)

# Captured before the autouse patch below rebinds them, so the calibration
# controls can prove the ambient state is real rather than coincidentally absent.
_REAL_OLLAMA_REACHABLE = ambient._ollama_reachable
_REAL_CLAUDE_LOGIN_DETECTED = ambient._claude_login_detected


@pytest.fixture(autouse=True)
def _isolate_ambient_discovery(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Keep host state out of ambient provider detection.

    Four channels are closed. Vendor API keys in the developer's environment
    (bare, ``OMNIGENT_``-prefixed, and the Vertex triple) are cleared, since
    detection reads each of them. A live Ollama accepting TCP on the probe
    port would be detected as a provider that shadows the configured one, so
    the reachability probe is pinned to ``False``. The Claude login detector
    is narrowed to its ``$HOME``-scoped credentials-file check, which tests
    can drive by writing that file under an isolated home, while the macOS
    Keychain fallback (a ``claude auth status`` subprocess that ignores
    ``$HOME``) is severed. The login-probe cache is cleared on both sides of
    the test, so a positive verdict cached by one test — its TTL outlives the
    test — can never be consumed by another.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: Iterator yielding once, with the caches cleared around the test.
    """
    for env_var in (*PROVIDER_ENV_VARS.values(), *_VERTEX_ENV_VARS):
        monkeypatch.delenv(env_var, raising=False)
        monkeypatch.delenv(f"OMNIGENT_{env_var}", raising=False)
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: False)
    monkeypatch.setattr(
        ambient,
        "_claude_login_detected",
        lambda: ambient.claude_auth_has_credential(ambient._claude_credentials_path()),
    )
    harness_install._LOGIN_PROBE_CACHE.clear()
    yield
    harness_install._LOGIN_PROBE_CACHE.clear()


@pytest.fixture
def real_ambient_probes() -> SimpleNamespace:
    """
    Expose the unpatched ambient probes for calibration assertions.

    A test that isolates ambient state should first prove the state would
    have been visible without the isolation; otherwise it passes vacuously on
    a machine where no Ollama runs and no Claude login exists.

    :returns: Namespace with ``ollama_reachable`` and ``claude_login_detected``
        bound to the real, unpatched probe functions.
    """
    return SimpleNamespace(
        ollama_reachable=_REAL_OLLAMA_REACHABLE,
        claude_login_detected=_REAL_CLAUDE_LOGIN_DETECTED,
    )
