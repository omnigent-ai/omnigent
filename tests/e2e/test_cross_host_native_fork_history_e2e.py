"""E2E regression: forking a native session across DIFFERENT hosts.

Journey: start a claude-native session bound to host A and run a turn (Claude
assigns a host-local transcript; the wrapper bridge reports its id via
``PATCH /v1/sessions``), then clone the session onto a DIFFERENT online host B
via the Clone dialog. The clone must open with the prior conversation history,
not a blank terminal.

Failure mode guarded here: the fork's one-shot resume directive
(``FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY``) names the source's transcript on
host A. An external fork is created UNBOUND — the target host is picked
afterwards via ``POST /v1/hosts/{host_id}/runners`` — so the directive is
stamped before the host is known and must be re-evaluated at bind time. Bound
to a different host it routes the runner into a doomed clone of a transcript
that isn't there (launches fresh, silently losing all history) and blocks the
cross-host-safe rebuild-from-items fallback, which requires the directive to
be absent. A managed fork already skips the directive for the same reason.

Contract asserted: a native fork bound to a host other than its source's must
NOT carry the host-local resume directive (while keeping the carry-history
marker), so the runner rebuilds history from the copied Omnigent items.

Scope: drives the real cross-host path — a real ``omnigent server``, two real
host daemons (separate ``HOME``\\ s => two distinct hosts), the real fork and
bind routes — and asserts on the persisted directive, the direct cause of the
history loss. Rendering the visible symptom (the blank terminal) additionally
needs an interactive Claude login anchored to the real ``$HOME``; that layer
is covered by the ``OMNIGENT_E2E_CLAUDE_NATIVE``-gated
``test_host_claude_native_fork_e2e``. Hermetic: dummy LLM key, no turn runs.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
    FORK_SOURCE_LABEL_KEY,
)

# Reuse the hermetic cross-host harness (real server + two real host daemons
# under isolated HOMEs, claude-native wrapper session creation and host
# binding) from the resume-picker cross-host suite.
from tests.e2e.test_native_resume_picker_cross_host_e2e import (
    _bind_to_host,
    _boot_server,
    _client,
    _create_wrapper_session,
    _spawn_host,
)


def _set_external_session_id(server_url: str, session_id: str, external_id: str) -> None:
    """Report the source's native session id via the real wrapper-bridge PATCH."""
    with _client() as c:
        resp = c.patch(
            f"{server_url}/v1/sessions/{session_id}",
            json={"external_session_id": external_id},
        )
        resp.raise_for_status()


def _fork_external(server_url: str, source_id: str, title: str) -> str:
    """Fork via the real route as the web Clone dialog does (external, unbound)."""
    with _client() as c:
        resp = c.post(
            f"{server_url}/v1/sessions/{source_id}/fork",
            json={"title": title},
            timeout=90.0,
        )
        resp.raise_for_status()
        body = resp.json()
    return str(body["id"])


def _session_body(server_url: str, session_id: str) -> dict:
    with _client() as c:
        resp = c.get(f"{server_url}/v1/sessions/{session_id}")
        resp.raise_for_status()
        return resp.json()


@dataclass
class _CrossHostForkEnv:
    """Booted server + host A + host B, with a native source bound to host A."""

    server_url: str
    host_a_id: str
    host_b_id: str
    home_b: Path
    source_id: str
    source_external_id: str


@pytest.fixture(scope="module")
def cross_host_fork_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_CrossHostForkEnv]:
    root = tmp_path_factory.mktemp("cross_host_fork")
    procs = []
    try:
        server_proc, url = _boot_server(root)
        procs.append(server_proc)
        host_a_proc, host_a, home_a = _spawn_host(root, "hostA", url)
        procs.append(host_a_proc)
        host_b_proc, host_b, home_b = _spawn_host(root, "hostB", url)
        procs.append(host_b_proc)

        source_id = _create_wrapper_session(url, "coding-session-on-host-A")
        # Simulates a completed native turn: Claude assigned a local transcript
        # on host A and the wrapper bridge reported its id. Set BEFORE the host
        # binds so no runner races the one-time external_session_id write.
        source_external_id = f"claude-src-{uuid.uuid4()}"
        _set_external_session_id(url, source_id, source_external_id)
        _bind_to_host(url, source_id, host_a, home_a / "wsA")

        yield _CrossHostForkEnv(
            server_url=url,
            host_a_id=host_a,
            host_b_id=host_b,
            home_b=home_b,
            source_id=source_id,
            source_external_id=source_external_id,
        )
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def test_cross_host_fork_does_not_carry_host_local_resume_directive(
    cross_host_fork_env: _CrossHostForkEnv,
) -> None:
    """A fork bound to a host other than the source's must not carry the
    source's host-local native-resume directive.
    """
    env = cross_host_fork_env

    # Fork as the web Clone dialog does (external, created unbound), then bind
    # to a DIFFERENT online host, as picking another host in the dialog does.
    fork_id = _fork_external(env.server_url, env.source_id, "fork-onto-host-B")
    _bind_to_host(env.server_url, fork_id, env.host_b_id, env.home_b / "wsB")

    source_body = _session_body(env.server_url, env.source_id)
    fork_body = _session_body(env.server_url, fork_id)
    fork_labels = fork_body.get("labels") or {}

    # Precondition: this really is a cross-host fork.
    assert source_body.get("host_id") == env.host_a_id
    assert fork_body.get("host_id") == env.host_b_id
    assert fork_body.get("host_id") != source_body.get("host_id"), (
        "test setup failed to bind the fork to a different host than the source"
    )

    assert fork_labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1", (
        f"fork should stay marked to carry history into the native harness; labels={fork_labels}"
    )

    carried = fork_labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
    assert carried is None, (
        "cross-host fork carries a host-local native-resume directive it cannot "
        f"honor on its bound host ({FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY}="
        f"{carried!r} names the source's transcript on host {env.host_a_id}, "
        f"while the fork is bound to host {env.host_b_id}). The runner would "
        "clone a transcript that isn't there and launch fresh, losing all "
        "prior conversation history; the directive must be dropped at bind "
        "time so the runner rebuilds history from the copied items. "
        f"fork labels={fork_labels}"
    )

    # Sanity: the copied-history / source-provenance plumbing stays intact.
    assert fork_labels.get(FORK_SOURCE_LABEL_KEY) == env.source_id
