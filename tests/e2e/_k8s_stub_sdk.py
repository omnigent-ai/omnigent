"""Stub of the optional ``kubernetes`` client for the sandbox disk-bounds e2e.

Materialized onto the server subprocess's PYTHONPATH by the kubernetes-sandbox
e2e tests. It satisfies the SDK surface the launcher touches and records every
Job manifest passed to ``create_namespaced_job`` (and every launch-token Secret
passed to ``create_namespaced_secret``) into
``$OMNIGENT_TEST_K8S_CAPTURE_FILE``. The pod
listing stays empty so the pod-ready wait gives up after the configured
``pod_ready_timeout_s`` — the manifest is captured before that wait.

Kept in its own module (no network calls of its own) so the security exfil
scan doesn't flag the API method names (``create_namespaced_secret``) sitting
next to the test's real HTTP client.
"""

from __future__ import annotations

# Env var naming the JSON file the stub SDK appends captured API calls to.
CAPTURE_ENV_VAR = "OMNIGENT_TEST_K8S_CAPTURE_FILE"

# Relative path -> source for each stub module written under the PYTHONPATH root.
STUB_FILES: dict[str, str] = {
    "kubernetes/__init__.py": (
        '"""Stub SDK: records what the launcher submits to the apiserver."""\n'
        "from . import client, config  # noqa: F401\n"
    ),
    "kubernetes/client/__init__.py": (
        "import json\n"
        "import os\n"
        "from types import SimpleNamespace\n"
        "\n"
        "from . import rest  # noqa: F401\n"
        "\n"
        "\n"
        "class Configuration:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "\n"
        "class ApiClient:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "    def close(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "def _capture(payload):\n"
        f'    path = os.environ.get("{CAPTURE_ENV_VAR}")\n'
        "    if not path:\n"
        "        return\n"
        "    records = []\n"
        "    if os.path.exists(path):\n"
        "        with open(path) as f:\n"
        "            records = json.load(f)\n"
        "    records.append(payload)\n"
        '    tmp = path + ".tmp"\n'
        '    with open(tmp, "w") as f:\n'
        "        json.dump(records, f)\n"
        "    os.replace(tmp, path)\n"
        "\n"
        "\n"
        "class CoreV1Api:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "    def create_namespaced_secret(self, namespace, body, **kw):\n"
        "        _capture(\n"
        '            {"call": "create_namespaced_secret",\n'
        '             "namespace": namespace, "manifest": body}\n'
        "        )\n"
        "        return SimpleNamespace()\n"
        "\n"
        "    def list_namespaced_pod(self, namespace, **kw):\n"
        "        return SimpleNamespace(items=[])\n"
        "\n"
        "    def read_namespaced_pod(self, name, namespace, **kw):\n"
        '        raise rest.ApiException(status=404, reason="NotFound")\n'
        "\n"
        "    def list_namespaced_event(self, namespace, **kw):\n"
        "        return SimpleNamespace(items=[])\n"
        "\n"
        "    def read_namespaced_pod_log(self, name, namespace, **kw):\n"
        '        return ""\n'
        "\n"
        "    def delete_namespaced_secret(self, name, namespace, **kw):\n"
        "        return SimpleNamespace()\n"
        "\n"
        "    def delete_namespaced_pod(self, name, namespace, **kw):\n"
        "        return SimpleNamespace()\n"
        "\n"
        "\n"
        "class BatchV1Api:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n"
        "\n"
        "    def create_namespaced_job(self, namespace, body, **kw):\n"
        "        _capture(\n"
        '            {"call": "create_namespaced_job", "namespace": namespace, "manifest": body}\n'
        "        )\n"
        "        return SimpleNamespace()\n"
        "\n"
        "    def delete_namespaced_job(self, name, namespace, **kw):\n"
        "        return SimpleNamespace()\n"
    ),
    "kubernetes/client/rest.py": (
        "class ApiException(Exception):\n"
        "    def __init__(self, status=None, reason=None, body=None):\n"
        '        super().__init__(f"({status}) Reason: {reason}")\n'
        "        self.status = status\n"
        "        self.reason = reason\n"
        "        self.body = body\n"
    ),
    "kubernetes/config/__init__.py": (
        "class ConfigException(Exception):\n"
        "    pass\n"
        "\n"
        "\n"
        "def load_incluster_config(client_configuration=None, **kw):\n"
        '    raise ConfigException("no in-cluster service account")\n'
        "\n"
        "\n"
        "def load_kube_config(config_file=None, client_configuration=None, **kw):\n"
        "    return None\n"
    ),
}
