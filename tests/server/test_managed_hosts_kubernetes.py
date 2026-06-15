"""Tests for the ``kubernetes`` branch of :func:`parse_sandbox_config`."""

from __future__ import annotations

import pytest

from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.server.managed_hosts import (
    KUBERNETES_MANAGED_TOKEN_TTL_S,
    parse_sandbox_config,
)


def _build_kubernetes_launcher(raw: object) -> KubernetesSandboxLauncher:
    """
    Parse a kubernetes ``sandbox`` config and run its launcher factory.

    The kubernetes client is installed in this venv, so the factory's
    lazy import resolves and ``KubernetesSandboxLauncher.__init__`` runs;
    construction is pure (no cluster I/O — the API client is built lazily
    in ``_load_core``), so the returned instance is safe to inspect for
    the captured constructor wiring.

    :param raw: The raw ``sandbox`` mapping to parse.
    :returns: The launcher the parsed config's factory builds.
    """
    cfg = parse_sandbox_config(raw)
    assert cfg is not None
    launcher = cfg.launcher_factory()
    assert isinstance(launcher, KubernetesSandboxLauncher)
    return launcher


def test_parse_minimal_kubernetes_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``provider: kubernetes`` + ``server_url`` is a complete config: the
    optional ``kubernetes:`` block is omitted, so every constructor field
    reaches the launcher as ``None`` (its own env-var fallbacks /
    defaults apply), and the config advertises managed launch with the
    kubernetes token TTL.
    """
    # A dev's ambient namespace override must not leak into the default
    # assertion below.
    monkeypatch.delenv("OMNIGENT_KUBERNETES_NAMESPACE", raising=False)
    cfg = parse_sandbox_config(
        {
            "provider": "kubernetes",
            # Trailing slash is normalized: the URL is interpolated into
            # `omnigent host --server <url>` and double slashes break joins.
            "server_url": "https://srv.example.com/",
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    # No platform lifetime cap on Pods; 7-day policy bound mirrors
    # Daytona/Islo.
    assert cfg.token_ttl_s == KUBERNETES_MANAGED_TOKEN_TTL_S
    # kubernetes is in PROVIDERS_WITH_MANAGED_LAUNCH, so the parsed config
    # advertises managed launch (drives /v1/info's capability flag).
    assert cfg.managed_launch_supported is True
    # The parsed provider is carried through so /v1/info can label the
    # web UI's option.
    assert cfg.provider == "kubernetes"

    launcher = cfg.launcher_factory()
    assert isinstance(launcher, KubernetesSandboxLauncher)
    # Everything omitted → None reaches the launcher, so its own
    # resolution (env override → default) applies, not a config-pinned ref.
    assert launcher._image_ref is None
    assert launcher._namespace is None
    assert launcher._env_names is None
    assert launcher._secret_name is None
    assert launcher._service_account is None
    assert launcher._node_selector is None
    # FIX-D: with no namespace configured, the launcher's resolved default
    # is the DEDICATED runner namespace (not the server's "omnigent") — the
    # overlay grants the server SA rights there, and the server namespace
    # would 403 + defeat the blast-radius split.
    assert launcher._resolve_namespace() == "omnigent-sandboxes"


def test_parse_full_kubernetes_config_threads_to_launcher() -> None:
    """
    The documented ``kubernetes:`` YAML shape parses into a config whose
    factory constructs a launcher carrying the configured image,
    env-passthrough names, namespace, Secret name, ServiceAccount, and
    node selector.
    """
    launcher = _build_kubernetes_launcher(
        {
            "provider": "kubernetes",
            "server_url": "https://srv.example.com/",
            "kubernetes": {
                "image": "ghcr.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "namespace": "omnigent-sandboxes",
                "secret_name": "omnigent-creds",
                "service_account": "omnigent-runner",
                "node_selector": {"disktype": "ssd", "gpu": "false"},
                "kubeconfig": "/etc/omnigent/kubeconfig",
                "in_cluster": False,
            },
        }
    )
    assert launcher._image_ref == "ghcr.io/me/omnigent-host:latest"
    # The launcher stores env names as a tuple internally.
    assert launcher._env_names == ("OPENAI_API_KEY", "GIT_TOKEN")
    assert launcher._namespace == "omnigent-sandboxes"
    assert launcher._secret_name == "omnigent-creds"
    assert launcher._service_account == "omnigent-runner"
    assert launcher._node_selector == {"disktype": "ssd", "gpu": "false"}
    # kubeconfig + in_cluster must thread through (the YAML path used to
    # drop them silently, defeating the out-of-cluster config option).
    assert launcher._kubeconfig == "/etc/omnigent/kubeconfig"
    assert launcher._in_cluster is False


def test_parse_kubernetes_in_cluster_true_threads_to_launcher() -> None:
    """
    ``in_cluster: true`` forces the in-cluster ServiceAccount config path
    (no kubeconfig fallback) — it must reach the launcher as ``True``.
    """
    launcher = _build_kubernetes_launcher(
        {
            "provider": "kubernetes",
            "server_url": "https://srv.example.com/",
            "kubernetes": {"in_cluster": True},
        }
    )
    assert launcher._in_cluster is True
    # kubeconfig omitted → None (its own env-var fallback applies).
    assert launcher._kubeconfig is None


def test_parse_kubernetes_factory_builds_launcher_instance() -> None:
    """The factory builds a fresh ``KubernetesSandboxLauncher``."""
    cfg = parse_sandbox_config({"provider": "kubernetes", "server_url": "https://s.example.com"})
    assert cfg is not None
    first = cfg.launcher_factory()
    second = cfg.launcher_factory()
    assert isinstance(first, KubernetesSandboxLauncher)
    assert isinstance(second, KubernetesSandboxLauncher)
    # Called per launch — a fresh instance each time, not a shared handle.
    assert first is not second


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        # kubernetes section present but not a mapping.
        (
            {"provider": "kubernetes", "server_url": "https://s", "kubernetes": "x"},
            "sandbox.kubernetes",
        ),
        # namespace present but not a non-empty string.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"namespace": "  "},
            },
            "sandbox.kubernetes.namespace",
        ),
        # secret_name present but not a non-empty string.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"secret_name": 5},
            },
            "sandbox.kubernetes.secret_name",
        ),
        # node_selector not a mapping at all.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"node_selector": "disktype=ssd"},
            },
            "sandbox.kubernetes.node_selector",
        ),
        # node_selector mapping with a non-string value.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"node_selector": {"disktype": 1}},
            },
            "sandbox.kubernetes.node_selector",
        ),
        # node_selector mapping with an empty key.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"node_selector": {"": "ssd"}},
            },
            "sandbox.kubernetes.node_selector",
        ),
        # env malformed (reuses the shared provider env helper).
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"env": "OPENAI"},
            },
            "sandbox.kubernetes.env",
        ),
        # in_cluster present but a string (YAML "true") — must NOT be
        # silently coerced; a wrong config source is a loud error.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"in_cluster": "true"},
            },
            "sandbox.kubernetes.in_cluster",
        ),
        # in_cluster present but an int — also rejected (1 is not a bool).
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"in_cluster": 1},
            },
            "sandbox.kubernetes.in_cluster",
        ),
        # kubeconfig present but not a non-empty string.
        (
            {
                "provider": "kubernetes",
                "server_url": "https://s",
                "kubernetes": {"kubeconfig": "  "},
            },
            "sandbox.kubernetes.kubeconfig",
        ),
    ],
)
def test_parse_invalid_kubernetes_config_fails_loud(raw: object, expected_fragment: str) -> None:
    """
    Malformed kubernetes config raises with the offending key named —
    this is what stops server startup on an operator typo instead of
    502-ing the first managed session.
    """
    with pytest.raises(ValueError) as exc:
        parse_sandbox_config(raw)
    assert expected_fragment in str(exc.value)
