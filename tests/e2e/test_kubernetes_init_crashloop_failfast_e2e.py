"""
End-to-end guard: a crash-looping workspace-prep init container must fail the
managed Kubernetes launch fast, with the init container's log tail attached.

Journey (operator + user):

1. An operator configures ``sandbox.provider: kubernetes`` on the server,
   pointing at a cluster whose sandbox namespace cannot reach the clone host
   (a default-deny NetworkPolicy is enough).
2. A user creates a managed session with a repository workspace
   (``POST /v1/sessions`` with ``host_type: "managed"`` and a repo-URL
   ``workspace``), which makes the server's Kubernetes launcher submit a Job
   and wait for its Pod.
3. ``git clone`` in the ``workspace-prep`` init container fails immediately
   and the kubelet restarts it: the Pod sits in phase ``Pending`` with the
   init container in ``CrashLoopBackOff`` — it will never come up.
4. The user watches the session's sandbox launch progress: it must fail
   within seconds, name the crash-looping container, and carry a tail of its
   log (the clone error) instead of polling out ``pod_ready_timeout_s`` with
   Pod events only.

The apiserver is unreachable from the test environment, so a stub
``kubernetes`` package on the server subprocess's PYTHONPATH stands in for
the cluster (see ``tests/e2e/_k8s_crashloop_stub_sdk``). Everything else is
real: the server process, its config parsing, the managed-session HTTP
journey, the launcher's start wait, and the failure-message builder.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.live_server import find_free_port
from tests.e2e._k8s_crashloop_server import spawn_server, terminate, wait_for_health
from tests.e2e._k8s_crashloop_stub_sdk import CLONE_ERROR_LINE

_POLL_INTERVAL_S = 0.5

# The repository workspace the user asks for — the clone the init container
# fails on.
_REPO_URL = "https://github.com/omnigent-ai/omnigent"

# Pod-ready budget: generous enough that a fail-fast well under it is
# unambiguous, small enough that the buggy poll-to-the-deadline path doesn't
# stall CI for the default 90s.
_POD_READY_TIMEOUT_S = 45

# The stub parks workspace-prep in CrashLoopBackOff ~3s after the Job is
# submitted, so a fail-fast start wait surfaces the failure within seconds;
# the buggy path cannot fail before the 45s deadline. 25s splits the two.
_FAILFAST_MAX_S = 25.0


def _create_managed_repo_session(base_url: str) -> str:
    """Drive the user journey: create a managed session with a repo workspace."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    assert agents, "no agents registered on the server to bind a session to"
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agents[0]["id"],
            "host_type": "managed",
            "workspace": _REPO_URL,
        },
        timeout=120.0,
    )
    assert response.status_code == 201, (
        f"managed session create failed: HTTP {response.status_code}: {response.text[:500]}"
    )
    session_id: str = response.json()["id"]
    return session_id


def _await_failed_launch(
    base_url: str, session_id: str, budget_s: float, log_path: Path
) -> tuple[float, str]:
    """Poll the session snapshot until its sandbox launch fails.

    :returns: ``(elapsed_s, error)`` — seconds from call start until the
        snapshot's ``sandbox_status`` reported stage ``failed``, and the
        failure detail shown to the user.
    """
    start = time.monotonic()
    deadline = start + budget_s
    stage = None
    while time.monotonic() < deadline:
        try:
            snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        except httpx.HTTPError:
            time.sleep(_POLL_INTERVAL_S)
            continue
        status = snapshot.get("sandbox_status") or {}
        stage = status.get("stage")
        if stage == "failed":
            return time.monotonic() - start, str(status.get("error") or "")
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"sandbox launch never reported failure within {budget_s}s "
        f"(last stage {stage!r}):\n{log_path.read_text()[-3000:]}"
    )


def test_init_crashloop_fails_fast_with_init_log_tail(tmp_path: Path) -> None:
    """An init crash loop fails the launch before the pod-ready deadline, and
    the failure names the init container and carries its log tail."""
    port = find_free_port()
    proc, log_path = spawn_server(tmp_path, port, _POD_READY_TIMEOUT_S)
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_for_health(proc, base_url, log_path)
        session_id = _create_managed_repo_session(base_url)
        elapsed, error = _await_failed_launch(
            base_url, session_id, _POD_READY_TIMEOUT_S + 45.0, log_path
        )
    finally:
        terminate(proc)

    problems: list[str] = []
    if elapsed >= _FAILFAST_MAX_S:
        problems.append(
            f"launch failure took {elapsed:.1f}s — the crash-looping workspace-prep "
            f"init container was not fail-fast detected and the start wait polled to "
            f"its {_POD_READY_TIMEOUT_S}s pod-ready deadline (90s in a default "
            f"deployment)"
        )
    if "workspace-prep" not in error:
        problems.append(f"launch failure does not name the failed init container: {error[:800]}")
    if CLONE_ERROR_LINE not in error:
        problems.append(
            "launch failure dropped the workspace-prep log tail — the user never "
            "sees the git clone error the init container printed (the apiserver "
            f"served it via read_namespaced_pod_log); error: {error[:800]}"
        )
    if problems:
        pytest.fail("\n".join(problems))
