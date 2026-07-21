"""Tests for :mod:`omnigent.server.managed_hosts`."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import click
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.base import render_host_config_write_command
from omnigent.onboarding.sandboxes.e2b import managed_token_ttl_s as e2b_managed_token_ttl_s
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.managed_hosts import (
    BOXLITE_MANAGED_TOKEN_TTL_S,
    DAYTONA_MANAGED_TOKEN_TTL_S,
    ISLO_MANAGED_TOKEN_TTL_S,
    KUBERNETES_MANAGED_TOKEN_TTL_S,
    MODAL_MANAGED_TOKEN_TTL_S,
    OPENSHELL_MANAGED_TOKEN_TTL_S,
    ManagedSandboxConfig,
    RepoWorkspace,
    host_resume_supported,
    launch_managed_host,
    parse_repo_workspace,
    parse_sandbox_config,
    relaunch_managed_host,
    resume_managed_host,
    terminate_managed_host,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import (
    FakeSandboxLauncher,
    HostStartInvocation,
    install_fake_boxlite_launcher,
    install_fake_daytona_launcher,
    install_fake_e2b_launcher,
    install_fake_islo_launcher,
    install_fake_kubernetes_launcher,
    install_fake_modal_launcher,
    install_fake_openshell_launcher,
)

pytestmark = pytest.mark.asyncio

_OWNER = "alice@example.com"


def _injected_config(
    fake: FakeSandboxLauncher,
    *,
    server_url: str = "https://srv.example.com",
    token_ttl_s: int = 3600,
    host_config: dict[str, object] | None = None,
) -> ManagedSandboxConfig:
    """
    Build a config that injects *fake* through the launcher-factory seam
    — the same way an embedding deployment injects a custom launcher.

    :param fake: The launcher every launch should use.
    :param server_url: Server URL the sandbox host dials back to.
    :param token_ttl_s: Launch-token lifetime in seconds.
    :param host_config: In-sandbox config.yaml content to forward, or ``None``.
    :returns: A ready :class:`ManagedSandboxConfig`.
    """
    return ManagedSandboxConfig(
        server_url=server_url,
        launcher_factory=lambda: fake,
        token_ttl_s=token_ttl_s,
        host_config=host_config,
    )


# ── parse_sandbox_config ────────────────────────────────────


def test_parse_absent_section_disables_managed_hosts() -> None:
    """No ``sandbox:`` section → managed hosts simply not configured."""
    assert parse_sandbox_config(None) is None


def test_parse_valid_modal_config_builds_image_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented modal YAML shape parses into a config whose factory
    constructs Modal launchers carrying the configured image — the
    pre-baked-image thread that makes managed startup fast.
    """
    cfg = parse_sandbox_config(
        {
            "provider": "modal",
            # Trailing slash is normalized: the URL is interpolated into
            # `omnigent host --server <url>` and double slashes break joins.
            "server_url": "https://srv.example.com/",
            "modal": {"image": "docker.io/me/omnigent-host:latest"},
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == MODAL_MANAGED_TOKEN_TTL_S
    # modal is in PROVIDERS_WITH_MANAGED_LAUNCH, so the parsed config
    # advertises managed launch (drives /v1/info's capability flag).
    assert cfg.managed_launch_supported is True
    # The parsed provider is carried through so /v1/info can label the
    # web UI's option ("Modal Sandbox").
    assert cfg.provider == "modal"
    # The factory resolves ModalSandboxLauncher at call time; substitute
    # the fake at that public seam to observe the constructor wiring.
    fake = FakeSandboxLauncher()
    install_fake_modal_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "docker.io/me/omnigent-host:latest"
    # No secrets configured → None reaches the launcher (its env-var
    # fallback applies), not an empty list.
    assert fake.secrets is None


def test_parse_modal_without_image_defaults_to_official(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: modal` + `server_url` is a complete config: the image is
    optional and defaults to the official prebaked host image (the
    launcher resolves env override / official default when constructed
    with image=None).
    """
    cfg = parse_sandbox_config({"provider": "modal", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_modal_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    # image=None → the launcher's own resolution (env var → official
    # default) applies, rather than a config-pinned ref.
    assert fake.image is None


def test_parse_non_modal_provider_yields_rejecting_factory() -> None:
    """
    lakebox configs parse (a deployment can stage config before
    managed-launch support lands), but their factory rejects with a 400
    naming the provider when a managed session is actually requested.
    """
    cfg = parse_sandbox_config({"provider": "lakebox", "server_url": "https://s.example.com"})
    assert cfg is not None
    # A staged provider must not advertise managed launch on /v1/info —
    # the web UI would offer a sandbox option every create rejects.
    assert cfg.managed_launch_supported is False
    # The provider is still parsed onto the config; /v1/info gates on
    # managed_launch_supported, so the name is not surfaced while staged.
    assert cfg.provider == "lakebox"
    with pytest.raises(HTTPException) as exc:
        cfg.launcher_factory()
    assert exc.value.status_code == 400
    assert "lakebox" in exc.value.detail


def test_parse_valid_daytona_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented daytona YAML shape parses into a config whose
    factory constructs Daytona launchers carrying the configured image
    and env-passthrough names, with the daytona token TTL (no platform
    lifetime cap; 7-day policy bound).
    """
    cfg = parse_sandbox_config(
        {
            "provider": "daytona",
            "server_url": "https://srv.example.com/",
            "daytona": {
                "image": "docker.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == DAYTONA_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    fake = FakeSandboxLauncher()
    install_fake_daytona_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "docker.io/me/omnigent-host:latest"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]


def test_parse_daytona_without_section_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: daytona` + `server_url` is a complete config: image and
    env are optional and reach the launcher as None (its own env-var
    fallbacks / official-image default apply).
    """
    cfg = parse_sandbox_config({"provider": "daytona", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_daytona_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image is None
    assert fake.env is None


def test_parse_valid_boxlite_cloud_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented boxlite YAML shape (cloud: remote ``boxlite serve``)
    parses into a config whose factory constructs boxlite launchers
    carrying the endpoint, image, and env-passthrough names, with the
    boxlite token TTL (no platform lifetime cap; 7-day policy bound).
    """
    cfg = parse_sandbox_config(
        {
            "provider": "boxlite",
            "server_url": "https://srv.example.com/",
            "boxlite": {
                "image": "docker.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "cloud": {"endpoint": "https://boxlite.example.com:8100"},
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == BOXLITE_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "boxlite"
    fake = FakeSandboxLauncher()
    install_fake_boxlite_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.endpoint == "https://boxlite.example.com:8100"
    assert fake.image == "docker.io/me/omnigent-host:latest"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]


def test_parse_boxlite_without_section_defaults_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: boxlite` + `server_url` is a complete config: the boxlite
    block is optional, so endpoint/image/env reach the launcher as None
    — LOCAL mode (embedded micro-VMs on the server host, no endpoint).
    """
    cfg = parse_sandbox_config({"provider": "boxlite", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_boxlite_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.endpoint is None
    assert fake.image is None
    assert fake.env is None


def test_parse_boxlite_local_customization_reaches_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `sandbox.boxlite.home_dir` + `registry` reach the launcher: a custom data
    dir and a private-registry block (credential env NAMES, never values).
    """
    cfg = parse_sandbox_config(
        {
            "provider": "boxlite",
            "server_url": "https://s.example.com",
            "boxlite": {
                "local": {
                    "home_dir": "/data/boxlite",
                    "registry": {
                        "host": "ghcr.io",
                        "username_env": "GHCR_USER",
                        "password_env": "GHCR_PAT",
                    },
                },
            },
        }
    )
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_boxlite_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.home_dir == "/data/boxlite"
    assert fake.registry == {
        "host": "ghcr.io",
        "username_env": "GHCR_USER",
        "password_env": "GHCR_PAT",
    }


def test_parse_valid_islo_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented islo YAML shape parses into a config whose factory
    constructs Islo launchers carrying image, env names, API override,
    and optional Islo sandbox sizing/profile fields.
    """
    cfg = parse_sandbox_config(
        {
            "provider": "islo",
            "server_url": "https://srv.example.com/",
            "islo": {
                "image": "docker.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "base_url": "https://api.islo.dev/",
                "gateway_profile": "default",
                "snapshot_name": "warm-host",
                "workdir": "/root/workspace",
                "vcpus": 4,
                "memory_mb": 8192,
                "disk_gb": 40,
                "idle_pause_after_s": 1200,
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == ISLO_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "islo"
    fake = FakeSandboxLauncher()
    install_fake_islo_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "docker.io/me/omnigent-host:latest"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]
    assert fake.base_url == "https://api.islo.dev/"
    assert fake.gateway_profile == "default"
    assert fake.snapshot_name == "warm-host"
    assert fake.workdir == "/root/workspace"
    assert fake.vcpus == 4
    assert fake.memory_mb == 8192
    assert fake.disk_gb == 40
    assert fake.idle_pause_after_s == 1200


def test_parse_islo_without_section_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: islo` + `server_url` is a complete config: optional
    constructor fields reach the launcher as None so its env-var
    fallbacks / official-image default apply.
    """
    cfg = parse_sandbox_config({"provider": "islo", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_islo_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image is None
    assert fake.env is None
    assert fake.base_url is None
    assert fake.gateway_profile is None
    assert fake.snapshot_name is None
    assert fake.workdir is None
    assert fake.vcpus is None
    assert fake.memory_mb is None
    assert fake.disk_gb is None
    assert fake.idle_pause_after_s == 900


def test_parse_islo_config_idle_pause_null_disables_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit null opts out of Islo's default idle pause policy."""
    cfg = parse_sandbox_config(
        {
            "provider": "islo",
            "server_url": "https://s.example.com",
            "islo": {"idle_pause_after_s": None},
        }
    )
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_islo_launcher(monkeypatch, fake)

    assert cfg.launcher_factory() is fake
    assert fake.idle_pause_after_s is None


def test_parse_valid_e2b_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented e2b YAML shape parses into a config whose factory
    constructs E2B launchers carrying the configured template name and
    env-passthrough names, with the e2b token TTL (24h cap → mirror
    Modal's 25h token lifetime).
    """
    cfg = parse_sandbox_config(
        {
            "provider": "e2b",
            "server_url": "https://srv.example.com/",
            "e2b": {
                "template": "omnigent-host",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == e2b_managed_token_ttl_s()
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "e2b"
    fake = FakeSandboxLauncher()
    install_fake_e2b_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.template == "omnigent-host"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]


def test_parse_e2b_without_section_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: e2b` + `server_url` is a complete config: template and
    env are optional and reach the launcher as None (its own env-var
    fallbacks / default-template apply).
    """
    cfg = parse_sandbox_config({"provider": "e2b", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_e2b_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.template is None
    assert fake.env is None


def test_parse_e2b_template_rejects_non_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A present-but-malformed e2b template fails loud at parse time."""
    with pytest.raises(ValueError, match=r"sandbox\.e2b\.template"):
        parse_sandbox_config(
            {
                "provider": "e2b",
                "server_url": "https://s.example.com",
                "e2b": {"template": ""},
            }
        )


def test_parse_valid_openshell_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented openshell YAML shape parses into a config whose
    factory constructs OpenShell launchers carrying image, env names,
    and the optional gateway cluster.
    """
    cfg = parse_sandbox_config(
        {
            "provider": "openshell",
            "server_url": "https://srv.example.com/",
            "openshell": {
                "image": "docker.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "cluster": "my-gateway",
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "https://srv.example.com"
    assert cfg.token_ttl_s == OPENSHELL_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "openshell"
    fake = FakeSandboxLauncher()
    install_fake_openshell_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "docker.io/me/omnigent-host:latest"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]
    assert fake.cluster == "my-gateway"


def test_parse_openshell_without_section_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    `provider: openshell` + `server_url` is a complete config: optional
    constructor fields reach the launcher as None so its env-var
    fallbacks / official-image default / active-gateway apply.
    """
    cfg = parse_sandbox_config({"provider": "openshell", "server_url": "https://s.example.com"})
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_openshell_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image is None
    assert fake.env is None
    assert fake.cluster is None


def test_parse_valid_kubernetes_config_builds_parameterized_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The documented kubernetes YAML shape parses into a config whose factory
    constructs Kubernetes launchers carrying namespace / Secret / SA / node
    selector / in-cluster / resources, with the 7-day token TTL.
    """
    cfg = parse_sandbox_config(
        {
            "provider": "kubernetes",
            "server_url": "http://omnigent.omnigent.svc.cluster.local/",
            "kubernetes": {
                "image": "ghcr.io/me/omnigent-host:latest",
                "env": ["OPENAI_API_KEY", "GIT_TOKEN"],
                "namespace": "omnigent-sandboxes",
                "secret_name": "omnigent-creds",
                "service_account": "omnigent-runner",
                "node_selector": {"omnigent.ai/runner-ready": "true"},
                "in_cluster": True,
                "resources": {"requests": {"cpu": "500m"}, "limits": {"memory": "8Gi"}},
            },
        }
    )
    assert cfg is not None
    assert cfg.server_url == "http://omnigent.omnigent.svc.cluster.local"
    assert cfg.token_ttl_s == KUBERNETES_MANAGED_TOKEN_TTL_S
    assert cfg.managed_launch_supported is True
    assert cfg.provider == "kubernetes"
    fake = FakeSandboxLauncher()
    install_fake_kubernetes_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.image == "ghcr.io/me/omnigent-host:latest"
    assert fake.env == ["OPENAI_API_KEY", "GIT_TOKEN"]
    assert fake.namespace == "omnigent-sandboxes"
    assert fake.secret_name == "omnigent-creds"
    assert fake.service_account == "omnigent-runner"
    assert fake.node_selector == {"omnigent.ai/runner-ready": "true"}
    assert fake.in_cluster is True
    assert fake.resources == {"requests": {"cpu": "500m"}, "limits": {"memory": "8Gi"}}


def test_parse_kubernetes_without_section_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    `provider: kubernetes` + `server_url` is a complete config: optional fields
    reach the launcher as None so its env-var fallbacks / defaults apply.
    """
    cfg = parse_sandbox_config(
        {"provider": "kubernetes", "server_url": "http://s.svc.cluster.local"}
    )
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_kubernetes_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.namespace is None
    assert fake.secret_name is None
    assert fake.in_cluster is None
    assert fake.resources is None


def test_parse_host_config_threads_verbatim_without_resolving_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A valid host_config lands on the parsed config verbatim, and its
    ``api_key_ref: env:`` reference is NOT resolved at parse time — the
    variable names sandbox environment, not server environment, so parsing
    must succeed with the variable unset on the server.
    """
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_LITELLM_API_KEY", raising=False)
    host_config = {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "default": ["pi"],
                "openai": {
                    "base_url": "http://litellm.litellm.svc.cluster.local/v1",
                    "api_key_ref": "env:LITELLM_API_KEY",
                    "wire_api": "chat",
                },
            }
        }
    }

    cfg = parse_sandbox_config(
        {"provider": "modal", "server_url": "https://s.example.com", "host_config": host_config}
    )

    assert cfg is not None
    assert cfg.host_config == host_config


def test_parse_absent_host_config_is_none() -> None:
    """No host_config key → nothing forwarded, existing configs unchanged."""
    cfg = parse_sandbox_config({"provider": "modal", "server_url": "https://s.example.com"})
    assert cfg is not None
    assert cfg.host_config is None


def test_parse_host_config_null_providers_fails_loud() -> None:
    """
    An explicit ``providers: null`` fails parse. Left through, the sandbox
    merge would write ``providers: null`` over any existing block and the
    harness would silently fall back to its own login — the exact
    degradation this parse exists to stop.
    """
    with pytest.raises(ValueError, match=r"sandbox\.host_config\.providers"):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "host_config": {"providers": None},
            }
        )


def test_parse_host_config_duplicate_default_fails_loud() -> None:
    """Duplicate defaults fail at server startup, before sandbox launch."""
    provider = {
        "kind": "gateway",
        "default": ["pi"],
        "openai": {
            "base_url": "https://gateway.example.com/v1",
            "api_key_ref": "env:GATEWAY_API_KEY",
        },
    }

    with pytest.raises(
        ValueError,
        match=r"sandbox\.host_config\.providers.*multiple providers.*'pi' family",
    ):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "host_config": {
                    "providers": {
                        "first": provider,
                        "second": provider,
                    }
                },
            }
        )


def test_parse_host_config_inline_api_key_fails_loud() -> None:
    """Literal provider credentials cannot ride in the managed host config."""
    with pytest.raises(ValueError, match=r"api_key_ref: env:VAR"):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "host_config": {
                    "providers": {
                        "openai": {
                            "kind": "key",
                            "openai": {
                                "base_url": "https://api.openai.com/v1",
                                "api_key": "sk-inline-secret",
                            },
                        }
                    }
                },
            }
        )


def test_parse_host_config_lossy_json_key_collision_fails_loud() -> None:
    """JSON key coercion cannot silently collapse distinct config entries."""
    with pytest.raises(ValueError, match=r"JSON-serializable"):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "host_config": {"metadata": {1: "integer", "1": "string"}},
            }
        )


@pytest.mark.parametrize(
    ("kubernetes_block", "expected_fragment"),
    [
        ({"namespace": "Bad_NS"}, "sandbox.kubernetes.namespace"),
        ({"node_selector": {"omnigent.ai/x": "Bad Value"}}, "node_selector"),
        ({"resources": {"requests": {"cpu": "not a quantity!"}}}, "valid Kubernetes quantity"),
        ({"resources": {"requests": {"disk": "1Gi"}}}, "unknown key"),
        ({"in_cluster": "yes"}, "must be a boolean"),
    ],
)
def test_parse_kubernetes_invalid_block_fails_loud(
    kubernetes_block: dict[str, object], expected_fragment: str
) -> None:
    """An operator typo in the kubernetes block fails parse loud, not at launch."""
    with pytest.raises(ValueError, match=expected_fragment):
        parse_sandbox_config(
            {
                "provider": "kubernetes",
                "server_url": "http://s.svc.cluster.local",
                "kubernetes": kubernetes_block,
            }
        )


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        # Non-mapping section.
        ("modal", "must be a mapping"),
        # Unknown / missing provider.
        ({"provider": "bogus", "server_url": "https://s"}, "sandbox.provider"),
        ({"server_url": "https://s"}, "sandbox.provider"),
        # Missing / empty server_url.
        ({"provider": "modal", "modal": {"image": "x"}}, "server_url"),
        ({"provider": "modal", "server_url": "  ", "modal": {"image": "x"}}, "server_url"),
        # modal section present but malformed.
        ({"provider": "modal", "server_url": "https://s", "modal": "x"}, "sandbox.modal"),
        (
            {"provider": "modal", "server_url": "https://s", "modal": {"image": "  "}},
            "sandbox.modal.image",
        ),
        # daytona section present but malformed.
        ({"provider": "daytona", "server_url": "https://s", "daytona": "x"}, "sandbox.daytona"),
        (
            {"provider": "daytona", "server_url": "https://s", "daytona": {"image": "  "}},
            "sandbox.daytona.image",
        ),
        (
            {"provider": "daytona", "server_url": "https://s", "daytona": {"env": "OPENAI"}},
            "sandbox.daytona.env",
        ),
        (
            {"provider": "daytona", "server_url": "https://s", "daytona": {"env": ["", "X"]}},
            "sandbox.daytona.env",
        ),
        # boxlite section present but malformed.
        ({"provider": "boxlite", "server_url": "https://s", "boxlite": "x"}, "sandbox.boxlite"),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"image": "  "}},
            "sandbox.boxlite.image",
        ),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"env": "OPENAI"}},
            "sandbox.boxlite.env",
        ),
        # boxlite mode blocks (local / cloud are mutually exclusive).
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"local": {}, "cloud": {"endpoint": "https://b"}},
            },
            "mutually exclusive",
        ),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"cloud": "x"}},
            "sandbox.boxlite.cloud",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"cloud": {"endpoint": "  "}},
            },
            "sandbox.boxlite.cloud.endpoint",
        ),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"local": "x"}},
            "sandbox.boxlite.local",
        ),
        # A bare `cloud:` / `local:` YAML key (value None) is malformed — it must
        # be rejected, not silently fall through to LOCAL mode (a `cloud:` typo
        # would otherwise run locally with no diagnostic).
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"cloud": None}},
            "sandbox.boxlite.cloud",
        ),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"local": None}},
            "sandbox.boxlite.local",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"local": {"home_dir": "  "}},
            },
            "sandbox.boxlite.local.home_dir",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"local": {"registry": "x"}},
            },
            "sandbox.boxlite.local.registry",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"local": {"registry": {"transport": "https"}}},
            },
            "sandbox.boxlite.local.registry.host",
        ),
        # M3: bearer token + basic auth both set (boxlite silently drops basic).
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {
                    "local": {
                        "registry": {"host": "ghcr.io", "token_env": "T", "password_env": "P"}
                    }
                },
            },
            "mutually exclusive",
        ),
        # M4: misplaced / unknown keys are rejected, not silently ignored.
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"endpoint": "https://b"},
            },
            "unknown key",
        ),
        (
            {"provider": "boxlite", "server_url": "https://s", "boxlite": {"bogus": 1}},
            "unknown key",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"cloud": {"endpoint": "https://b", "bogus": 1}},
            },
            "unknown key",
        ),
        (
            {
                "provider": "boxlite",
                "server_url": "https://s",
                "boxlite": {"local": {"registry": {"host": "ghcr.io", "passwrod_env": "P"}}},
            },
            "unknown key",
        ),
        # islo section present but malformed.
        ({"provider": "islo", "server_url": "https://s", "islo": "x"}, "sandbox.islo"),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"image": "  "}},
            "sandbox.islo.image",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"env": "OPENAI"}},
            "sandbox.islo.env",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"env": ["", "X"]}},
            "sandbox.islo.env",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"base_url": "  "}},
            "sandbox.islo.base_url",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"vcpus": 0}},
            "sandbox.islo.vcpus",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"memory_mb": "large"}},
            "sandbox.islo.memory_mb",
        ),
        (
            {"provider": "islo", "server_url": "https://s", "islo": {"idle_pause_after_s": 0}},
            "sandbox.islo.idle_pause_after_s",
        ),
        (
            {
                "provider": "islo",
                "server_url": "https://s",
                "islo": {"idle_pause_after_s": "900"},
            },
            "sandbox.islo.idle_pause_after_s",
        ),
        # openshell section present but malformed.
        (
            {"provider": "openshell", "server_url": "https://s", "openshell": "x"},
            "sandbox.openshell",
        ),
        (
            {"provider": "openshell", "server_url": "https://s", "openshell": {"image": "  "}},
            "sandbox.openshell.image",
        ),
        (
            {"provider": "openshell", "server_url": "https://s", "openshell": {"env": ["", "X"]}},
            "sandbox.openshell.env",
        ),
        (
            {"provider": "openshell", "server_url": "https://s", "openshell": {"cluster": "  "}},
            "sandbox.openshell.cluster",
        ),
        # host_config present but malformed (provider-agnostic top-level key).
        (
            {"provider": "modal", "server_url": "https://s", "host_config": "providers: {}"},
            "sandbox.host_config",
        ),
        (
            {"provider": "modal", "server_url": "https://s", "host_config": {"providers": "x"}},
            "sandbox.host_config.providers",
        ),
        # An invalid provider entry (bad kind) is caught by the same parser
        # omnigent itself uses — inside the sandbox this would degrade
        # silently, so parse time is the only loud failure point.
        (
            {
                "provider": "modal",
                "server_url": "https://s",
                "host_config": {"providers": {"litellm": {"kind": "bogus"}}},
            },
            "sandbox.host_config.providers",
        ),
        # yaml.safe_load turns an unquoted date into datetime.date, which the
        # per-launch json.dumps cannot take — must fail startup, not launches.
        (
            {
                "provider": "modal",
                "server_url": "https://s",
                "host_config": {"last_rotated": datetime.date(2024, 1, 1)},
            },
            "JSON-serializable",
        ),
    ],
)
def test_parse_invalid_config_fails_loud(raw: object, expected_fragment: str) -> None:
    """
    Malformed config raises with the offending key named — this is
    what stops server startup on an operator typo instead of 502-ing
    the first managed session.
    """
    with pytest.raises(ValueError, match="") as exc:
        parse_sandbox_config(raw)
    assert expected_fragment in str(exc.value)


# ── parse_repo_workspace ────────────────────────────────────


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        # Plain https URL — default branch, name from the last segment.
        (
            "https://github.com/org/repo",
            RepoWorkspace(url="https://github.com/org/repo", branch=None, repo_name="repo"),
        ),
        # `.git` suffix stripped from the directory name, kept in the URL.
        (
            "https://github.com/org/repo.git#release-1.2",
            RepoWorkspace(
                url="https://github.com/org/repo.git",
                branch="release-1.2",
                repo_name="repo",
            ),
        ),
        # scp-style ssh form.
        (
            "git@github.com:org/repo.git",
            RepoWorkspace(url="git@github.com:org/repo.git", branch=None, repo_name="repo"),
        ),
        # Branches with slashes are legal git refs.
        (
            "https://github.com/org/repo#feature/x",
            RepoWorkspace(url="https://github.com/org/repo", branch="feature/x", repo_name="repo"),
        ),
    ],
)
def test_parse_repo_workspace_accepts_url_forms(workspace: str, expected: RepoWorkspace) -> None:
    """
    The documented ``<repo>[#<branch>]`` grammar parses into the
    validated spec the clone step consumes — URL, pinned branch, and
    the clone directory name all come from here, so a wrong field
    means a wrong `git clone` invocation.
    """
    assert parse_repo_workspace(workspace) == expected


@pytest.mark.parametrize(
    ("workspace", "expected_fragment"),
    [
        # Absolute paths are the EXTERNAL form — a path points at
        # nothing in a sandbox that doesn't exist yet.
        ("/tmp/w", "not a supported repository URL"),
        # Bare org/repo shorthand is UI-side sugar, never API surface.
        ("org/repo", "not a supported repository URL"),
        # No repo path at all.
        ("https://github.com", "not a usable https repository URL"),
        ("git@github.com", "not a usable ssh repository URL"),
        # Commit SHAs would land the agent on a detached HEAD.
        ("https://github.com/org/repo#" + "a" * 40, "not a commit SHA"),
        # Empty / malformed branch fragments.
        ("https://github.com/org/repo#", "must name a branch"),
        ("https://github.com/org/repo#-flag", "not a valid git branch name"),
        ("https://github.com/org/repo#a..b", "not a valid git branch name"),
        # A second '#' means the branch itself contains '#' —
        # unsupported in the fragment form.
        ("https://github.com/org/repo#a#b", "not a valid git branch name"),
        ("https://github.com/org/repo#a b", "must not contain whitespace"),
    ],
)
def test_parse_repo_workspace_rejects_malformed(workspace: str, expected_fragment: str) -> None:
    """
    Malformed workspaces fail loud at parse time with the offense
    named — this is what turns into the create's 422 instead of a
    mid-provision clone error inside a half-launched sandbox.
    """
    with pytest.raises(ValueError, match="") as exc:
        parse_repo_workspace(workspace)
    assert expected_fragment in str(exc.value)


# ── GET /v1/info: managed_sandboxes_enabled ─────────────────


def _capability_probe_app(
    db_uri: str,
    tmp_path: Path,
    sandbox_config: ManagedSandboxConfig | None,
) -> FastAPI:
    """
    Build a real app wired with *sandbox_config* to probe ``GET /v1/info``.

    Minimal store wiring — the probe handler reads only the
    ``sandbox_config`` closure, but the app factory needs real stores.

    :param db_uri: SQLite connection URI for the app's stores.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :param sandbox_config: The sandbox config under test, or ``None``
        when managed hosts are not configured.
    :returns: The assembled FastAPI app.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        sandbox_config=sandbox_config,
    )


@pytest.mark.parametrize(
    ("sandbox_raw", "expected", "expected_provider"),
    [
        # Launch-capable provider configured → the web UI may offer the
        # sandbox option, labeled with the provider name ("Modal Sandbox").
        ({"provider": "modal", "server_url": "https://s.example.com"}, True, "modal"),
        # No `sandbox:` section → a managed create would 400; the option
        # must not be advertised and no provider is named.
        (None, False, None),
        # advertising it would offer a create path that always fails, so
        # the option is hidden and the provider stays unnamed.
        ({"provider": "lakebox", "server_url": "https://s.example.com"}, False, None),
        # Daytona has managed-launch support like modal → offered and
        # named so the UI can label it ("Daytona Sandbox").
        ({"provider": "daytona", "server_url": "https://s.example.com"}, True, "daytona"),
        # Islo has managed-launch support too → offered and provider-labeled.
        ({"provider": "islo", "server_url": "https://s.example.com"}, True, "islo"),
    ],
)
async def test_info_reports_managed_sandboxes_capability(
    db_uri: str,
    tmp_path: Path,
    sandbox_raw: dict[str, object] | None,
    expected: bool,
    expected_provider: str | None,
) -> None:
    """
    ``GET /v1/info`` advertises managed sandboxes iff the wired config
    can actually serve a managed launch, and names the backing provider
    (``sandbox_provider``) so the web UI can label the option per
    provider — but only when the option is actually offered.
    """
    app = _capability_probe_app(db_uri, tmp_path, parse_sandbox_config(sandbox_raw))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed_sandboxes_enabled"] is expected
    # The provider name is surfaced only when the option is offered; a
    # staged/absent config leaks nothing (provider stays None), so the
    # daytona/none cases never name a backend.
    assert body["sandbox_provider"] == expected_provider


async def test_info_reports_enabled_for_injected_custom_launcher(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    The embedding seam: a directly-constructed config (custom launcher
    factory, no YAML) defaults to advertising managed launch — the
    deployment's factory IS the support. With no provider named, the UI
    falls back to the generic "New Sandbox" label (``sandbox_provider``
    is None).
    """
    config = ManagedSandboxConfig(
        server_url="https://s.example.com",
        launcher_factory=lambda: FakeSandboxLauncher(),
        token_ttl_s=3600,
    )
    app = _capability_probe_app(db_uri, tmp_path, config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["managed_sandboxes_enabled"] is True
    # No provider set on the injected config → the UI keeps the generic
    # label rather than inventing a name.
    assert body["sandbox_provider"] is None


# ── launch_managed_host ─────────────────────────────────────


async def test_launch_success_registers_host_and_returns_workspace(db_uri: str) -> None:
    """
    Golden path: provision → pre-register the host row with its token
    → start host → host online.

    The launcher arrives through the config's factory seam (no
    patching), and the fake's ``on_host_start`` connects exactly as
    the real tunnel would after validating the launch token
    (``upsert_on_connect`` against the pre-registered row), so the
    online poll observes a genuine hosts-table transition.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    result = await launch_managed_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
    )

    assert fake.prepared is True
    # The workspace was created in the sandbox's home and returned.
    assert result.workspace == "/root/workspace"
    assert any("mkdir -p /root/workspace" in cmd for cmd in fake.commands)
    # The start command dials back to the configured server URL.
    start = fake.host_starts[0]
    assert "--server https://srv.example.com" in start.command
    assert result.host_id == start.host_id
    # The hosts row carries the managed binding with full content; the
    # provider comes from the LAUNCHER (not config), so injected custom
    # launchers record their own name.
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.user_id == _OWNER
    assert host.name == start.host_name
    assert host.status == "online"
    assert host.sandbox_provider == "modal"
    assert host.sandbox_id == "sb-fake-1"
    # The token injected into the sandbox is the one whose digest was
    # stored: resolving it (the tunnel's auth path) yields this host,
    # which also proves it is unexpired.
    resolved = host_store.resolve_launch_token(start.host_id, start.token)
    assert resolved is not None
    assert resolved.host_id == result.host_id
    # Nothing was torn down on the success path.
    assert fake.terminated == []


async def test_launch_materializes_host_config_before_host_start(db_uri: str) -> None:
    """
    A configured host_config is written into the sandbox strictly BEFORE
    ``omnigent host`` starts — the whole point of the injection is that the
    host boots with its providers already on disk.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    host_config: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}

    await launch_managed_host(
        config=_injected_config(fake, host_config=host_config),
        owner=_OWNER,
        host_store=host_store,
    )

    write_index = fake.commands.index(render_host_config_write_command(host_config))
    host_index = next(i for i, cmd in enumerate(fake.commands) if "omnigent host --server" in cmd)
    assert write_index < host_index


async def test_resume_rematerializes_host_config_before_host_restart(db_uri: str) -> None:
    """
    Waking a dormant sandbox re-runs the config write before re-execing the
    host — resume_managed_host bypasses _arm_and_start_host, so this is a
    distinct wiring point, and re-materializing is what lets an operator's
    host_config change land on the next wake without a new sandbox.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register, can_resume=True)
    host_config: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}
    config = _injected_config(fake, host_config=host_config)

    result = await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)
    host_store.set_offline(result.host_id)
    commands_before = len(fake.commands)

    await resume_managed_host(result.host_id, host_store, config)

    assert fake.resumed == ["sb-fake-1"]
    resumed_commands = fake.commands[commands_before:]
    write_index = resumed_commands.index(render_host_config_write_command(host_config))
    host_index = next(
        i for i, cmd in enumerate(resumed_commands) if "omnigent host --server" in cmd
    )
    assert write_index < host_index


async def test_launch_without_host_config_writes_no_config(db_uri: str) -> None:
    """No host_config → the launch issues no config-write command at all."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    await launch_managed_host(config=_injected_config(fake), owner=_OWNER, host_store=host_store)

    assert not any(cmd.startswith("python3 -c") for cmd in fake.commands)


async def test_launch_without_host_config_supports_legacy_start_host_signature(
    db_uri: str,
) -> None:
    """
    A deployment-injected launcher whose ``start_host`` override predates the
    ``host_config`` parameter keeps launching when no host_config is set —
    the kwarg is omitted entirely rather than passed as ``None``.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    class _LegacySignatureLauncher(FakeSandboxLauncher):
        """Overrides start_host with the pre-host_config explicit signature."""

        def start_host(
            self,
            sandbox_id: str,
            *,
            token: str,
            host_id: str,
            host_name: str,
            server_url: str,
            repo_url: str | None = None,
            repo_branch: str | None = None,
            repo_name: str | None = None,
            on_stage: Callable[[str], None] | None = None,
        ) -> str:
            return super().start_host(
                sandbox_id,
                token=token,
                host_id=host_id,
                host_name=host_name,
                server_url=server_url,
                repo_url=repo_url,
                repo_branch=repo_branch,
                repo_name=repo_name,
                on_stage=on_stage,
            )

    fake = _LegacySignatureLauncher(on_host_start=_register)

    result = await launch_managed_host(
        config=_injected_config(fake), owner=_OWNER, host_store=host_store
    )

    [start] = fake.host_starts
    assert result.host_id == start.host_id


async def test_launch_with_injected_custom_launcher(db_uri: str) -> None:
    """
    The embedding seam end to end: a deployment-defined launcher (a
    provider name the YAML path doesn't even know) drives the whole
    managed flow, and its provider is what lands on the host row — so
    teardown later dispatches back to the same custom launcher.
    """
    host_store = HostStore(db_uri)

    class _AcmeLauncher(FakeSandboxLauncher):
        """Custom launcher under a deployment-private provider name."""

        provider: ClassVar[str] = "acme-cloud"

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _AcmeLauncher(on_host_start=_register)
    config = _injected_config(fake)

    result = await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)

    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.sandbox_provider == "acme-cloud"
    assert host.sandbox_id == "sb-fake-1"

    # Teardown resolves the launcher through the same config factory
    # (provider matches the row) — the custom launcher's terminate runs.
    await terminate_managed_host(host, host_store, config)
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.get_host(result.host_id) is None


async def test_launch_unsupported_yaml_provider_rejects_before_provisioning(
    db_uri: str,
) -> None:
    """
    A staged-but-unimplemented YAML provider (lakebox) fails with a 400
    naming the provider BEFORE any provisioning happens.
    """
    config = parse_sandbox_config({"provider": "lakebox", "server_url": "https://s.example.com"})
    assert config is not None
    host_store = HostStore(db_uri)
    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)
    assert exc.value.status_code == 400
    assert "lakebox" in exc.value.detail
    # No host row was pre-registered.
    assert host_store.list_hosts(_OWNER) == []


async def test_launch_provision_failure_maps_to_502(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A provider failure before anything exists (preflight) maps to a
    502 with the provider's message, and leaves no host row and
    nothing to terminate.
    """
    fake = FakeSandboxLauncher()

    def _fail_prepare() -> None:
        """Simulate missing provider credentials."""
        raise click.ClickException("No Modal credentials found.")

    monkeypatch.setattr(fake, "prepare", _fail_prepare)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake), owner=_OWNER, host_store=host_store
        )
    assert exc.value.status_code == 502
    assert "No Modal credentials found." in exc.value.detail
    assert host_store.list_hosts(_OWNER) == []
    assert fake.terminated == []


async def test_launch_host_start_failure_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A failure AFTER provisioning must clean up: terminate the sandbox
    (no orphaned paid compute) and delete the pre-registered host row
    (the minted token must not stay valid, and a never-started host
    must not linger in the picker).
    """
    fake = FakeSandboxLauncher(fail_on_host_start=True)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake), owner=_OWNER, host_store=host_store
        )
    assert exc.value.status_code == 502
    assert "simulated in-sandbox host start failure" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []


async def test_launch_non_click_exception_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A raw (non-Click, non-HTTP) exception during host start — a
    provider SDK error or a network failure from the in-sandbox exec —
    must trigger the same cleanup: terminate the sandbox and delete the
    host row. If the cleanup handler only caught ClickException, the
    sandbox would leak running until the provider's lifetime cap and
    the armed token would stay resolvable.
    """

    def _raise_sdk_error(invocation: HostStartInvocation) -> None:
        raise RuntimeError("simulated provider SDK failure")

    fake = FakeSandboxLauncher(on_host_start=_raise_sdk_error)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake), owner=_OWNER, host_store=host_store
        )
    assert exc.value.status_code == 502
    assert "simulated provider SDK failure" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []


async def test_launch_online_timeout_terminates_and_deletes_host(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A host that never registers (e.g. bad image, can't reach the
    server) times out with a 502 pointing at the in-sandbox log, and
    cleans up the sandbox + host row (which revokes the token).
    """
    # No on_host_start → the host never registers.
    fake = FakeSandboxLauncher()
    # Shrink the polling budget so the timeout path runs in
    # milliseconds; production values are module constants read at
    # call time.
    monkeypatch.setattr("omnigent.server.managed_hosts.MANAGED_HOST_ONLINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr("omnigent.server.managed_hosts._ONLINE_POLL_INTERVAL_S", 0.01)
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake), owner=_OWNER, host_store=host_store
        )
    assert exc.value.status_code == 502
    assert "did not come online" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []
    # The start command DID run (the failure was registration, not
    # startup), so its minted token exists — and must be dead.
    assert (
        host_store.resolve_launch_token(fake.host_starts[0].host_id, fake.host_starts[0].token)
        is None
    )


async def test_launch_with_repo_clones_into_workspace(db_uri: str) -> None:
    """
    A repository-URL workspace is cloned inside the sandbox BEFORE the
    host starts, and the cloned directory (not the bare workspace root)
    is what the session binds as its workspace.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)

    result = await launch_managed_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        repo=parse_repo_workspace("https://github.com/org/myrepo.git#release-1.2"),
    )

    # The session workspace is the clone directory, named after the repo.
    assert result.workspace == "/root/workspace/myrepo"
    # The exact clone invocation: branch-pinned, single-branch, `--`
    # separating options from the user-supplied URL. A drift here means
    # the sandbox clones the wrong thing (or interprets the URL as a
    # flag).
    clone_cmd = (
        "git clone --branch release-1.2 --single-branch "
        "-- https://github.com/org/myrepo.git /root/workspace/myrepo"
    )
    assert clone_cmd in fake.commands
    # Clone runs before the host starts — the workspace must be ready
    # by the time the runner can launch on the registered host.
    host_start_index = next(i for i, c in enumerate(fake.commands) if "omnigent host" in c)
    assert fake.commands.index(clone_cmd) < host_start_index
    assert fake.terminated == []


async def test_launch_clone_failure_terminates_and_deletes_host(db_uri: str) -> None:
    """
    A failed clone (bad URL, missing branch, private repo) cleans up
    exactly like a host-start failure — sandbox terminated, host row
    (and its token) deleted — and the 502 names the repository so the
    create error tells the user WHAT didn't clone.
    """
    fake = FakeSandboxLauncher(fail_on_command="git clone")
    host_store = HostStore(db_uri)

    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake),
            owner=_OWNER,
            host_store=host_store,
            repo=parse_repo_workspace("https://github.com/org/private#main"),
        )
    assert exc.value.status_code == 502
    assert "failed to clone repository 'https://github.com/org/private'" in exc.value.detail
    assert "'main'" in exc.value.detail
    assert fake.terminated == ["sb-fake-1"]
    assert host_store.list_hosts(_OWNER) == []
    # The host never started — the clone failed first.
    assert fake.host_starts == []


class _EntrypointFakeLauncher(FakeSandboxLauncher):
    """
    An entrypoint-as-host fake (like the kubernetes launcher): ``provision``
    only RESERVES the sandbox id (no box created), and the host is started by a
    ``start_host`` override — not the exec-model base default.

    Records the ``start_host`` call and, to prove the token is armed BEFORE the
    host starts, captures whether the token already resolves at call time (then
    simulates the host dialing back).
    """

    provider: ClassVar[str] = "kubernetes"

    def __init__(self, host_store: HostStore) -> None:
        super().__init__()
        self._host_store = host_store
        self.start_calls: list[dict[str, object]] = []
        self.token_resolved_at_start: bool = False

    def provision(self, name: str) -> str:
        """Reserve a sandbox id (no box created); recorded + deterministic."""
        self.provisioned_names.append(name)
        return f"omnigent-pod-{len(self.provisioned_names)}"

    def run(self, sandbox_id: str, command: str, *, check: bool = True):
        """The entrypoint model never execs in — the base default is overridden."""
        raise AssertionError("entrypoint launcher must not exec via run()")

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        host_config: dict[str, object] | None = None,
        on_stage=None,
    ) -> str:
        """Record the call, prove the token already resolves, and connect."""
        self.start_calls.append(
            {
                "sandbox_id": sandbox_id,
                "token": token,
                "host_id": host_id,
                "server_url": server_url,
                "repo_url": repo_url,
                "repo_name": repo_name,
            }
        )
        # The token was registered before start_host, so it resolves now.
        self.token_resolved_at_start = (
            self._host_store.resolve_launch_token(host_id, token) is not None
        )
        # Simulate the host's entrypoint dialing back over the tunnel.
        self._host_store.upsert_on_connect(host_id=host_id, name=host_name, user_id=_OWNER)
        return f"/home/omnigent/workspace/{repo_name}" if repo_name else "/home/omnigent/workspace"


async def test_launch_entrypoint_provider_arms_token_before_launch_host(db_uri: str) -> None:
    """
    Entrypoint-as-host seam: the uniform launch path reserves the sandbox id via
    provision(), registers the token, THEN calls start_host (never run) — so the
    host authenticates the moment its entrypoint dials back, with no race.
    """
    host_store = HostStore(db_uri)
    fake = _EntrypointFakeLauncher(host_store)

    result = await launch_managed_host(
        config=_injected_config(fake),
        owner=_OWNER,
        host_store=host_store,
        repo=parse_repo_workspace("https://github.com/org/repo.git#main"),
    )

    # start_host ran once, with the reserved id and repo info.
    assert len(fake.start_calls) == 1
    call = fake.start_calls[0]
    assert call["sandbox_id"] == "omnigent-pod-1"
    assert call["server_url"] == "https://srv.example.com"
    assert call["repo_url"] == "https://github.com/org/repo.git"
    assert call["repo_name"] == "repo"
    # The token was already resolvable when start_host ran (no dial-back race).
    assert fake.token_resolved_at_start is True
    # The workspace (cloned dir) is returned and the host is online + bound.
    assert result.workspace == "/home/omnigent/workspace/repo"
    host = host_store.get_host(result.host_id)
    assert host is not None
    assert host.status == "online"
    assert host.sandbox_provider == "kubernetes"
    assert host.sandbox_id == "omnigent-pod-1"


async def test_launch_entrypoint_provider_cleans_up_on_launch_failure(db_uri: str) -> None:
    """
    A start_host failure tears the sandbox down (by the reserved id) and deletes
    the host row, exactly like the exec path.
    """
    host_store = HostStore(db_uri)

    class _Failing(_EntrypointFakeLauncher):
        def start_host(self, sandbox_id: str, **kwargs: object) -> str:
            raise click.ClickException("pod could not be scheduled")

    fake = _Failing(host_store)
    with pytest.raises(HTTPException) as exc:
        await launch_managed_host(
            config=_injected_config(fake), owner=_OWNER, host_store=host_store
        )
    assert exc.value.status_code == 502
    assert "pod could not be scheduled" in exc.value.detail
    # The reserved sandbox was terminated and no host row survives.
    assert fake.terminated == ["omnigent-pod-1"]
    assert host_store.list_hosts(_OWNER) == []


# ── relaunch_managed_host ───────────────────────────────────


async def test_relaunch_rolls_sandbox_generation_under_same_host(db_uri: str) -> None:
    """
    A relaunch terminates the dead generation, provisions a fresh
    sandbox, and re-arms the SAME host row: identity (host_id, name,
    owner) stable, sandbox id rolled, and the NEW token resolving
    while the old one no longer does — a stale token resolving would
    let a dead sandbox's leaked credential impersonate the new host.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)
    gen1 = host_store.get_host(first.host_id)
    assert gen1 is not None
    gen1_token = fake.host_starts[0].token

    relaunched = await relaunch_managed_host(config=config, host=gen1, host_store=host_store)

    # Same identity, new generation: the session's host binding (which
    # references host_id) survives the roll.
    assert relaunched.host_id == first.host_id
    assert relaunched.workspace == "/root/workspace"
    assert fake.terminated == ["sb-fake-1"]
    host = host_store.get_host(first.host_id)
    assert host is not None
    assert host.sandbox_id == "sb-fake-2"
    assert host.name == gen1.name
    assert host.user_id == _OWNER
    # Generation 2 authenticated with a NEW token; generation 1's is
    # revoked by the re-arm (its digest no longer matches anything).
    gen2_token = fake.host_starts[1].token
    assert gen2_token != gen1_token
    resolved = host_store.resolve_launch_token(fake.host_starts[1].host_id, gen2_token)
    assert resolved is not None and resolved.host_id == first.host_id
    assert host_store.resolve_launch_token(fake.host_starts[0].host_id, gen1_token) is None


async def test_relaunch_failure_keeps_host_row_and_revokes_token(db_uri: str) -> None:
    """
    A FAILED relaunch must not delete the durable host row — deleting
    it would null the session's host binding (FK SET NULL) and make
    the session permanently unrelaunchable. The new sandbox is torn
    down and the armed token revoked, so nothing of the failed
    generation stays live; a later message retries against the kept
    row.
    """
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host connecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = FakeSandboxLauncher(on_host_start=_register)
    config = _injected_config(fake)
    first = await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)
    gen1 = host_store.get_host(first.host_id)
    assert gen1 is not None

    fake.fail_on_host_start = True
    with pytest.raises(HTTPException) as exc:
        await relaunch_managed_host(config=config, host=gen1, host_store=host_store)

    assert exc.value.status_code == 502
    # Both the dead generation 1 and the failed generation 2 sandboxes
    # were terminated — nothing leaks until the provider lifetime cap.
    assert fake.terminated == ["sb-fake-1", "sb-fake-2"]
    # The row SURVIVES the failure (contrast the first-launch failure
    # tests, which delete it), so the session binding stays relaunchable.
    host = host_store.get_host(first.host_id)
    assert host is not None
    # No credential of ANY generation is live: gen 1's was replaced by
    # the re-arm, and the re-armed token was revoked by the failure
    # cleanup (revoke_launch_token — covered directly in the host-store
    # suite). Gen 1's raw token is the only one observable here (the
    # failed start never executed), so assert on it.
    assert (
        host_store.resolve_launch_token(fake.host_starts[0].host_id, fake.host_starts[0].token)
        is None
    )


async def test_relaunch_rejects_unconfigured_provider(db_uri: str) -> None:
    """
    A provider mismatch (the ``sandbox:`` config changed since launch)
    fails the relaunch with a clear 400 instead of aiming another
    provider's terminate/provision at the recorded sandbox id.
    """
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="8369cb15e751573a1ee641d5fa09c70a",
        name="managed-mismatch",
        user_id=_OWNER,
        token="tok",
        provider="daytona",
        sandbox_id="dt-1",
        token_expires_at=now_epoch() + 3600,
    )

    fake = FakeSandboxLauncher()  # provider "modal" != row's "daytona"
    with pytest.raises(HTTPException) as exc:
        await relaunch_managed_host(
            config=_injected_config(fake), host=host, host_store=host_store
        )

    assert exc.value.status_code == 400
    assert "daytona" in exc.value.detail
    # Nothing was provisioned or terminated against the mismatched row.
    assert fake.provisioned_names == []
    assert fake.terminated == []


# ── resume_managed_host ─────────────────────────────────────


class _IsloFakeLauncher(FakeSandboxLauncher):
    """Fake launcher carrying Islo's provider label for managed resume tests."""

    provider: ClassVar[str] = "islo"


async def test_host_resume_supported_requires_resumable_matching_launcher(db_uri: str) -> None:
    """The wake gate requires matching provider, sandbox id, and ``can_resume``."""
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="292a6322075a34e482fde44975da10f3",
        name="managed-resume-gate",
        user_id=_OWNER,
        token="tok-resume-gate",
        provider="islo",
        sandbox_id="sb-resume-gate",
        token_expires_at=now_epoch() + 3600,
    )

    resumable = _IsloFakeLauncher(can_resume=True)
    assert host_resume_supported(host, _injected_config(resumable)) is True

    non_resumable = _IsloFakeLauncher(can_resume=False)
    assert host_resume_supported(host, _injected_config(non_resumable)) is False

    mismatched = FakeSandboxLauncher(can_resume=True)  # provider "modal"
    assert host_resume_supported(host, _injected_config(mismatched)) is False

    no_sandbox = host_store.register_managed_host(
        host_id="0c3d744a455047df9a3c0acf432d08dd",
        name="managed-resume-no-sandbox",
        user_id=_OWNER,
        token="tok-resume-no-sandbox",
        provider="islo",
        sandbox_id="sb-temp",
        token_expires_at=now_epoch() + 3600,
    )
    no_sandbox.sandbox_id = None
    assert host_resume_supported(no_sandbox, _injected_config(resumable)) is False


async def test_resume_managed_host_wakes_same_sandbox_and_refreshes_token(db_uri: str) -> None:
    """A resumable managed host wakes in place under the same sandbox id."""
    host_store = HostStore(db_uri)

    def _register(invocation: HostStartInvocation) -> None:
        """Simulate the sandbox host reconnecting over the tunnel."""
        host_store.upsert_on_connect(
            host_id=invocation.host_id,
            name=invocation.host_name,
            user_id=_OWNER,
        )

    fake = _IsloFakeLauncher(on_host_start=_register, can_resume=True)
    config = _injected_config(fake)
    first = await launch_managed_host(config=config, owner=_OWNER, host_store=host_store)
    host = host_store.get_host(first.host_id)
    assert host is not None
    assert host.sandbox_provider == "islo"
    assert host.sandbox_id == "sb-fake-1"
    first_token = fake.host_starts[0].token

    host_store.set_offline(first.host_id)
    assert host_resume_supported(host_store.get_host(first.host_id), config) is True

    await resume_managed_host(first.host_id, host_store, config)

    assert fake.resumed == ["sb-fake-1"]
    assert len(fake.provisioned_names) == 1
    woke = host_store.get_host(first.host_id)
    assert woke is not None
    assert woke.status == "online"
    assert woke.sandbox_provider == "islo"
    assert woke.sandbox_id == "sb-fake-1"
    second_token = fake.host_starts[1].token
    assert second_token != first_token
    assert host_store.resolve_launch_token(fake.host_starts[0].host_id, first_token) is None
    resolved = host_store.resolve_launch_token(fake.host_starts[1].host_id, second_token)
    assert resolved is not None and resolved.host_id == first.host_id


async def test_resume_managed_host_force_wakes_fresh_online_row(db_uri: str) -> None:
    """A local missing-tunnel wake can bypass stale cross-replica DB freshness."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id="62d4405ba38711fe34bebfeb5a7adaf2",
        name="managed-resume-force",
        user_id=_OWNER,
        token="tok-resume-force",
        provider="islo",
        sandbox_id="sb-resume-force",
        token_expires_at=now_epoch() + 3600,
    )
    host_store.upsert_on_connect(
        host_id="62d4405ba38711fe34bebfeb5a7adaf2",
        name="managed-resume-force",
        user_id=_OWNER,
    )
    assert host_store.is_online("62d4405ba38711fe34bebfeb5a7adaf2") is True
    fake = _IsloFakeLauncher(can_resume=True)

    await resume_managed_host(
        "62d4405ba38711fe34bebfeb5a7adaf2", host_store, _injected_config(fake), force=True
    )

    assert fake.resumed == ["sb-resume-force"]
    assert len(fake.host_starts) == 1
    assert (
        host_store.resolve_launch_token("62d4405ba38711fe34bebfeb5a7adaf2", "tok-resume-force")
        is None
    )
    resolved = host_store.resolve_launch_token(
        "62d4405ba38711fe34bebfeb5a7adaf2", fake.host_starts[0].token
    )
    assert resolved is not None and resolved.host_id == "62d4405ba38711fe34bebfeb5a7adaf2"


async def test_resume_managed_host_noops_for_non_resumable_provider(db_uri: str) -> None:
    """Non-resumable providers fall through without mutating the host row."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id="249d058fbcde7b2ce941479cdb8c82d7",
        name="managed-resume-noop",
        user_id=_OWNER,
        token="tok-resume-noop",
        provider="modal",
        sandbox_id="sb-resume-noop",
        token_expires_at=now_epoch() + 3600,
    )
    fake = FakeSandboxLauncher(can_resume=False)

    await resume_managed_host(
        "249d058fbcde7b2ce941479cdb8c82d7", host_store, _injected_config(fake)
    )

    assert fake.resumed == []
    assert fake.host_starts == []
    host = host_store.get_host("249d058fbcde7b2ce941479cdb8c82d7")
    assert host is not None
    assert host.status == "offline"
    assert host.sandbox_id == "sb-resume-noop"
    assert (
        host_store.resolve_launch_token("249d058fbcde7b2ce941479cdb8c82d7", "tok-resume-noop")
        is not None
    )


async def test_resume_managed_host_failure_preserves_existing_row_and_token(db_uri: str) -> None:
    """A failed wake leaves the dormant host retryable."""
    host_store = HostStore(db_uri)
    host_store.register_managed_host(
        host_id="efbef7dede7be6577770cbb1287992f2",
        name="managed-resume-fail",
        user_id=_OWNER,
        token="tok-resume-fail",
        provider="islo",
        sandbox_id="sb-resume-fail",
        token_expires_at=now_epoch() + 3600,
    )
    fake = _IsloFakeLauncher(can_resume=True, fail_on_resume=True)

    with pytest.raises(HTTPException) as exc:
        await resume_managed_host(
            "efbef7dede7be6577770cbb1287992f2", host_store, _injected_config(fake)
        )

    assert exc.value.status_code == 502
    assert "managed host wake failed" in exc.value.detail
    assert fake.host_starts == []
    host = host_store.get_host("efbef7dede7be6577770cbb1287992f2")
    assert host is not None
    assert host.status == "offline"
    assert host.sandbox_id == "sb-resume-fail"
    assert (
        host_store.resolve_launch_token("efbef7dede7be6577770cbb1287992f2", "tok-resume-fail")
        is not None
    )


# ── terminate_managed_host ──────────────────────────────────


async def test_terminate_managed_host_terminates_and_deletes_row(db_uri: str) -> None:
    """
    Cleanup terminates the provider sandbox and deletes the host row —
    one operation that removes the host from the picker AND revokes
    its launch token.
    """
    fake = FakeSandboxLauncher()
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="62a91eb065624754c6a6dfb5869dd7e8",
        name="managed-term1",
        user_id=_OWNER,
        token="tok-term-1",
        provider="modal",
        sandbox_id="sb-term-1",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_managed_host(host, host_store, _injected_config(fake))

    assert fake.terminated == ["sb-term-1"]
    assert host_store.get_host("62a91eb065624754c6a6dfb5869dd7e8") is None
    assert (
        host_store.resolve_launch_token("62a91eb065624754c6a6dfb5869dd7e8", "tok-term-1") is None
    )


async def test_terminate_managed_host_deletes_row_even_when_terminate_fails(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Best-effort contract: a provider termination failure neither
    propagates nor blocks the row deletion (the provider's lifetime
    cap reaps the sandbox; the credential must die now).
    """
    fake = FakeSandboxLauncher()

    def _explode(sandbox_id: str) -> None:
        """Simulate a provider API failure during termination."""
        raise click.ClickException("provider unavailable")

    monkeypatch.setattr(fake, "terminate", _explode)
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="057e7fa3f1cdb40c0ec393a3d42affc7",
        name="managed-term2",
        user_id=_OWNER,
        token="tok-term-2",
        provider="modal",
        sandbox_id="sb-term-2",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_managed_host(host, host_store, _injected_config(fake))

    assert host_store.get_host("057e7fa3f1cdb40c0ec393a3d42affc7") is None
    assert (
        host_store.resolve_launch_token("057e7fa3f1cdb40c0ec393a3d42affc7", "tok-term-2") is None
    )


async def test_terminate_managed_host_skips_mismatched_provider(db_uri: str) -> None:
    """
    A config change between launch and teardown (current launcher's
    provider ≠ the provider recorded on the row) must NOT aim the new
    provider's terminate at a stale sandbox id — the sandbox is left
    to its lifetime cap, but the row still dies (token revoked, no
    picker ghost). Also covers config=None (section removed).
    """
    fake = FakeSandboxLauncher()  # provider "modal"
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="487212fd2b157b6ab6a6d6d3ef06ce5b",
        name="managed-term3",
        user_id=_OWNER,
        token="tok-term-3",
        # Row launched under a provider the current config doesn't run.
        provider="acme-cloud",
        sandbox_id="sb-term-3",
        token_expires_at=now_epoch() + 3600,
    )

    await terminate_managed_host(host, host_store, _injected_config(fake))
    # No cross-provider terminate was attempted.
    assert fake.terminated == []
    assert host_store.get_host("487212fd2b157b6ab6a6d6d3ef06ce5b") is None
    assert (
        host_store.resolve_launch_token("487212fd2b157b6ab6a6d6d3ef06ce5b", "tok-term-3") is None
    )

    # config=None behaves the same: row deleted, nothing terminated.
    host2 = host_store.register_managed_host(
        host_id="b114bf90a8fd155ce6007c3bb262aa79",
        name="managed-term4",
        user_id=_OWNER,
        token="tok-term-4",
        provider="modal",
        sandbox_id="sb-term-4",
        token_expires_at=now_epoch() + 3600,
    )
    await terminate_managed_host(host2, host_store, None)
    assert host_store.get_host("b114bf90a8fd155ce6007c3bb262aa79") is None


def test_parse_modal_secrets_thread_to_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``sandbox.modal.secrets`` names reach the launcher constructor —
    the path that injects the deployment's harness LLM credentials
    into every managed sandbox.
    """
    cfg = parse_sandbox_config(
        {
            "provider": "modal",
            "server_url": "https://s.example.com",
            "modal": {"secrets": ["omnigent-llm", "gateway-extras"]},
        }
    )
    assert cfg is not None
    fake = FakeSandboxLauncher()
    install_fake_modal_launcher(monkeypatch, fake)
    assert cfg.launcher_factory() is fake
    assert fake.secrets == ["omnigent-llm", "gateway-extras"]
    # secrets without image: the official-image default still applies.
    assert fake.image is None


@pytest.mark.parametrize(
    "secrets",
    [
        "omnigent-llm",  # scalar, not a list
        ["omnigent-llm", 7],  # non-string entry
        ["  "],  # empty name
    ],
)
def test_parse_modal_secrets_malformed_fails_loud(secrets: object) -> None:
    """A present-but-malformed secrets value stops startup with the key named."""
    with pytest.raises(ValueError, match=r"sandbox\.modal\.secrets"):
        parse_sandbox_config(
            {
                "provider": "modal",
                "server_url": "https://s.example.com",
                "modal": {"secrets": secrets},
            }
        )
