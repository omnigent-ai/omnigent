"""Stub ``kubernetes`` client simulating a crash-looping workspace-prep init.

Materialized onto the server subprocess's PYTHONPATH by
``test_kubernetes_init_crashloop_failfast_e2e``. It satisfies the SDK surface
the launcher touches and plays back what a real apiserver reports while a
Job Pod's ``workspace-prep`` init container fails its ``git clone`` and is
restarted by the kubelet: the Pod stays in phase ``Pending`` with
``init_container_statuses[0].state.waiting.reason == "CrashLoopBackOff"``
(the exit recorded in ``last_state.terminated``), Pod events carry the
kubelet's ``BackOff`` line, and the init container's log tail carries the
clone error. The Pod never reaches ``Running`` and never reaches phase
``Failed`` during the launch window (``restartPolicy: OnFailure``).

Kept in its own module (no network calls of its own) so the security exfil
scan doesn't flag the API method names (``create_namespaced_secret``) sitting
next to the test's real HTTP client.
"""

from __future__ import annotations

import textwrap

# The init container's own output, served by ``read_namespaced_pod_log`` for
# the ``workspace-prep`` container — the diagnosis the launch error is
# expected to carry.
CLONE_ERROR_LINE = (
    "fatal: unable to access 'https://github.com/omnigent-ai/omnigent/': "
    "Could not resolve host: github.com"
)

# Seconds after Job creation before the simulated kubelet has seen the first
# clone failure and parks the init container in CrashLoopBackOff. Before
# that the init container reports as running (first attempt in flight).
CRASHLOOP_AFTER_S = 3.0

_CLIENT_MODULE = textwrap.dedent(
    '''
    """Stub SDK client: plays back a Pod whose init container crash-loops."""

    import time
    from types import SimpleNamespace

    from . import rest  # noqa: F401

    _CRASHLOOP_AFTER_S = {crashloop_after_s}

    _CLONE_LOG = (
        "Cloning into '/home/omnigent/workspace/omnigent'...\\n"
        {clone_error_line}
        "\\n"
    )

    # One simulated Job/Pod per server process: the Job name lands here on
    # create_namespaced_job and every pod read replays its current state.
    _state = {{"job_name": None, "created_at": None}}


    class Configuration:
        def __init__(self, *a, **k):
            pass


    class ApiClient:
        def __init__(self, *a, **k):
            pass

        def close(self):
            pass


    def _pod_name():
        return _state["job_name"] + "-x7k2p"


    def _pod():
        elapsed = time.monotonic() - _state["created_at"]
        crash_looping = elapsed >= _CRASHLOOP_AFTER_S
        if crash_looping:
            init_state = SimpleNamespace(
                waiting=SimpleNamespace(
                    reason="CrashLoopBackOff",
                    message=(
                        "back-off 20s restarting failed container=workspace-prep "
                        "pod=" + _pod_name() + "_omnigent-sandboxes"
                    ),
                ),
                running=None,
                terminated=None,
            )
            last_state = SimpleNamespace(
                terminated=SimpleNamespace(exit_code=128, reason="Error")
            )
            restart_count = 1 + int((elapsed - _CRASHLOOP_AFTER_S) // 20)
        else:
            init_state = SimpleNamespace(
                waiting=None, running=SimpleNamespace(started_at=None), terminated=None
            )
            last_state = SimpleNamespace(terminated=None)
            restart_count = 0
        return SimpleNamespace(
            metadata=SimpleNamespace(name=_pod_name(), deletion_timestamp=None),
            status=SimpleNamespace(
                phase="Pending",
                conditions=[],
                init_container_statuses=[
                    SimpleNamespace(
                        name="workspace-prep",
                        restart_count=restart_count,
                        state=init_state,
                        last_state=last_state,
                    )
                ],
                container_statuses=[
                    SimpleNamespace(
                        name="host",
                        restart_count=0,
                        state=SimpleNamespace(
                            waiting=SimpleNamespace(
                                reason="PodInitializing", message=None
                            ),
                            running=None,
                            terminated=None,
                        ),
                        last_state=SimpleNamespace(terminated=None),
                    )
                ],
            ),
        )


    class CoreV1Api:
        def __init__(self, *a, **k):
            pass

        def create_namespaced_secret(self, namespace, body, **kw):
            return SimpleNamespace()

        def list_namespaced_pod(self, namespace, **kw):
            if _state["job_name"] is None:
                return SimpleNamespace(items=[])
            return SimpleNamespace(items=[_pod()])

        def read_namespaced_pod(self, name, namespace, **kw):
            if _state["job_name"] is None:
                raise rest.ApiException(status=404, reason="NotFound")
            return _pod()

        def list_namespaced_event(self, namespace, **kw):
            if _state["job_name"] is None:
                return SimpleNamespace(items=[])
            return SimpleNamespace(
                items=[
                    SimpleNamespace(
                        reason="Scheduled",
                        message=(
                            "Successfully assigned omnigent-sandboxes/"
                            + _pod_name()
                            + " to node-1"
                        ),
                    ),
                    SimpleNamespace(
                        reason="Started", message="Started container workspace-prep"
                    ),
                    SimpleNamespace(
                        reason="BackOff",
                        message=(
                            "Back-off restarting failed container workspace-prep "
                            "in pod " + _pod_name() + "_omnigent-sandboxes"
                        ),
                    ),
                ]
            )

        def read_namespaced_pod_log(self, name, namespace, container=None, **kw):
            if container == "workspace-prep":
                return _CLONE_LOG
            return ""

        def delete_namespaced_secret(self, name, namespace, **kw):
            return SimpleNamespace()

        def delete_namespaced_pod(self, name, namespace, **kw):
            return SimpleNamespace()


    class V1DeleteOptions:
        def __init__(self, *a, **k):
            pass


    class BatchV1Api:
        def __init__(self, *a, **k):
            pass

        def create_namespaced_job(self, namespace, body, **kw):
            _state["job_name"] = body["metadata"]["name"]
            _state["created_at"] = time.monotonic()
            return SimpleNamespace()

        def delete_namespaced_job(self, name, namespace, **kw):
            return SimpleNamespace()
    '''
).format(
    crashloop_after_s=repr(CRASHLOOP_AFTER_S),
    clone_error_line=repr(CLONE_ERROR_LINE),
)

# Relative path -> source for each stub module written under the PYTHONPATH root.
STUB_FILES: dict[str, str] = {
    "kubernetes/__init__.py": (
        '"""Stub SDK: plays back a Pod whose init container crash-loops."""\n'
        "from . import client, config  # noqa: F401\n"
    ),
    "kubernetes/client/__init__.py": _CLIENT_MODULE,
    "kubernetes/client/rest.py": textwrap.dedent(
        '''
        class ApiException(Exception):
            def __init__(self, status=None, reason=None, body=None):
                super().__init__("(" + str(status) + ") Reason: " + str(reason))
                self.status = status
                self.reason = reason
                self.body = body
        '''
    ),
    "kubernetes/config/__init__.py": textwrap.dedent(
        '''
        class ConfigException(Exception):
            pass


        def load_incluster_config(client_configuration=None, **kw):
            raise ConfigException("no in-cluster service account")


        def load_kube_config(config_file=None, client_configuration=None, **kw):
            return None
        '''
    ),
}
