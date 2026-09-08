"""Unit tests for omnigent.inner.credential_proxy.

These cover the parent-side runtime: secret resolution from
``env`` / ``file`` / ``command`` sources, the default swap-on-access
model (nothing injected into the sandbox), and the opt-in synthetic
placeholder minting for ``inject_env`` entries. The end-to-end swap /
injection through a real sandbox + proxy is covered in
``tests/inner/sandbox/test_egress_e2e.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.credential_proxy import (
    SYNTHETIC_CREDENTIAL_PREFIX,
    CredentialProxyRuntime,
    DatabricksProfileTokenProvider,
    RefreshingSecretProvider,
    _prepare_databricks_runtime,
    prepare_credential_proxy_runtime,
)
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
    DatabricksProfileBinding,
    DatabricksProxySpec,
)


def _bearer_entry(
    host: str, *, env_var: str, source: CredentialSourceSpec
) -> CredentialProxyEntry:
    """
    Build a bearer-scheme entry that injects its placeholder into an env var.

    :param host: Bound host, e.g. ``"jira.example.com"``.
    :param env_var: Env var name to receive the synthetic, e.g. ``"JIRA"``.
    :param source: Secret source to resolve in the parent.
    :returns: A populated :class:`CredentialProxyEntry`.
    """
    return CredentialProxyEntry(
        host=host,
        scheme="bearer",
        source=source,
        inject_env=[env_var],
    )


def test_swap_on_access_entry_injects_nothing() -> None:
    """The default (no ``inject_env``) resolves the secret but injects nothing.

    This is the swap-on-access contract: the real secret reaches the
    proxy rewrite rule, but the sandbox env stays empty and no synthetic
    placeholder is minted (there is no placeholder to leak-guard because
    the sandbox never holds one). If injection regressed to always-on,
    ``helper_env_updates`` would be non-empty here; if minting regressed
    to always-on, ``rule.synthetic`` would be set.
    """
    spec = CredentialProxySpec(
        entries=[
            CredentialProxyEntry(
                host="github.com",
                scheme="basic",
                source=CredentialSourceSpec(kind="env", env="OA_SECRET"),
                username="x-access-token",
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={"OA_SECRET": "real-secret"})

    # Swap-on-access puts nothing in the sandbox env.
    assert runtime.helper_env_updates == {}
    assert len(runtime.rewrites) == 1
    rule = runtime.rewrites[0]
    # No env injection => no placeholder minted; the proxy injects the
    # real credential directly on a bound-host request.
    assert rule.synthetic is None
    assert rule.real_secret == "real-secret"
    assert rule.host == "github.com"
    assert rule.scheme == "basic"
    assert rule.username == "x-access-token"


def test_env_source_resolves_and_mints_synthetic() -> None:
    """An ``inject_env`` entry resolves the real secret and mints a placeholder.

    Proves the placeholder (not the real secret) lands in the helper env
    and that the rewrite rule carries the real secret for the proxy. If
    resolution broke, the rewrite rule's real_secret would be wrong; if
    minting broke, the synthetic would not carry the recognizable prefix.
    """
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "jira.example.com",
                env_var="JIRA_TOKEN",
                source=CredentialSourceSpec(kind="env", env="OA_SECRET"),
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={"OA_SECRET": "real-jira-secret"})

    synthetic = runtime.helper_env_updates["JIRA_TOKEN"]
    # The sandbox only ever sees the synthetic placeholder, never the
    # real secret. If env injection regressed, the real secret would
    # leak into the helper env here.
    assert synthetic.startswith(SYNTHETIC_CREDENTIAL_PREFIX)
    assert "real-jira-secret" not in synthetic

    assert len(runtime.rewrites) == 1
    rule = runtime.rewrites[0]
    # The proxy rule pairs the exact placeholder the sandbox holds with
    # the real upstream secret — the swap only works if these match.
    assert rule.synthetic == synthetic
    assert rule.real_secret == "real-jira-secret"
    assert rule.host == "jira.example.com"
    assert rule.scheme == "bearer"


def test_shared_env_reuses_placeholder_across_hosts() -> None:
    source = CredentialSourceSpec(kind="env", env="CURSOR_SECRET")
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry("api.cursor.com", env_var="CURSOR_API_KEY", source=source),
            _bearer_entry("api2.cursor.sh", env_var="CURSOR_API_KEY", source=source),
            _bearer_entry("agent.cursor.sh", env_var="CURSOR_API_KEY", source=source),
        ]
    )

    runtime = prepare_credential_proxy_runtime(
        spec, parent_env={"CURSOR_SECRET": "real-cursor-secret"}
    )

    synthetic = runtime.helper_env_updates["CURSOR_API_KEY"]
    assert synthetic.startswith(SYNTHETIC_CREDENTIAL_PREFIX)
    assert {rule.synthetic for rule in runtime.rewrites} == {synthetic}
    assert {rule.real_secret for rule in runtime.rewrites} == {"real-cursor-secret"}


def test_file_source_resolves_secret(tmp_path: Path) -> None:
    """A ``file`` source reads the secret from disk, stripped of whitespace.

    A failure here means the file contents didn't reach the rewrite rule,
    so the proxy would forward a wrong/blank credential upstream.
    """
    secret_file = tmp_path / "token.txt"
    secret_file.write_text("  file-secret\n", encoding="utf-8")
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "api.example.com",
                env_var="API_TOKEN",
                source=CredentialSourceSpec(kind="file", path=str(secret_file)),
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    # Trailing newline / leading spaces must be stripped so the upstream
    # gets exactly the token, not a token with embedded whitespace.
    assert runtime.rewrites[0].real_secret == "file-secret"


def test_command_source_resolves_secret() -> None:
    """A ``command`` source captures stdout as the secret.

    Uses a real subprocess (``python -c``) rather than a mock so the
    test exercises the actual ``subprocess.run`` path. If command
    resolution regressed (wrong stream, missing strip), the real_secret
    would not equal the printed value.
    """
    command = f"{sys.executable} -c \"print('cmd-secret')\""
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "svc.example.com",
                env_var="SVC_TOKEN",
                source=CredentialSourceSpec(kind="command", command=command),
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env=dict(os.environ))
    assert runtime.rewrites[0].real_secret == "cmd-secret"


@pytest.mark.parametrize(
    "source,parent_env,expected_fragment",
    [
        (CredentialSourceSpec(kind="env", env="MISSING"), {}, "missing or empty"),
        (CredentialSourceSpec(kind="file", path="/no/such/file"), {}, "does not exist"),
        (
            CredentialSourceSpec(kind="command", command="exit 7"),
            {},
            "exited 7",
        ),
    ],
)
def test_source_resolution_fails_loud(
    source: CredentialSourceSpec,
    parent_env: dict[str, str],
    expected_fragment: str,
) -> None:
    """Unresolvable sources raise rather than minting a blank placeholder.

    Fail-loud is the security property here: a silently-empty secret
    would let the sandbox authenticate as nobody (or, worse, mask a
    misconfiguration). Each case asserts the specific failure reason.
    """
    spec = CredentialProxySpec(
        entries=[_bearer_entry("h.example.com", env_var="T", source=source)]
    )
    with pytest.raises(ValueError, match=expected_fragment):
        prepare_credential_proxy_runtime(spec, parent_env=parent_env)


def test_gh_basic_shape_injects_env_for_api_host_only() -> None:
    """gh_basic injects GH env vars only for the API host; git is swap-on-access.

    Mirrors what the parser emits for ``gh_basic``: the API host gets
    ``GH_TOKEN``/``GITHUB_TOKEN`` injection (token scheme) because ``gh``
    won't call without a local token, while the git host authenticates
    purely via swap-on-access (basic scheme, nothing injected). A
    regression that injected the git host's credential, or dropped the
    API host's env injection, would break one of the two GitHub paths.
    """
    source = CredentialSourceSpec(kind="env", env="GH_PAT")
    spec = CredentialProxySpec(
        entries=[
            CredentialProxyEntry(
                host="github.com",
                scheme="basic",
                source=source,
                username="x-access-token",
            ),
            CredentialProxyEntry(
                host="api.github.com",
                scheme="token",
                source=source,
                inject_env=["GH_TOKEN", "GITHUB_TOKEN"],
            ),
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={"GH_PAT": "ghp_real"})

    # Both gh env vars carry the *same* synthetic as the api.github.com
    # rewrite rule, so ``gh api`` sends a placeholder the proxy can swap.
    api_rule = next(r for r in runtime.rewrites if r.host == "api.github.com")
    assert runtime.helper_env_updates["GH_TOKEN"] == api_rule.synthetic
    assert runtime.helper_env_updates["GITHUB_TOKEN"] == api_rule.synthetic
    assert api_rule.scheme == "token"
    assert api_rule.real_secret == "ghp_real"

    # The git host is pure swap-on-access: nothing injected, no synthetic
    # minted. Only the API host's two gh env vars are present.
    git_rule = next(r for r in runtime.rewrites if r.host == "github.com")
    assert git_rule.scheme == "basic"
    assert git_rule.synthetic is None
    assert git_rule.real_secret == "ghp_real"
    assert set(runtime.helper_env_updates) == {"GH_TOKEN", "GITHUB_TOKEN"}


# ---------------------------------------------------------------------------
# databricks_cli
# ---------------------------------------------------------------------------


class _FakeConfig:
    """Stand-in for ``databricks.sdk.config.Config`` in provider tests."""

    def __init__(self, host: str, tokens: list[str]) -> None:
        self.host = host
        self._tokens = tokens
        self.authenticate_calls = 0

    def authenticate(self) -> dict[str, str]:
        idx = min(self.authenticate_calls, len(self._tokens) - 1)
        self.authenticate_calls += 1
        return {"Authorization": f"Bearer {self._tokens[idx]}"}


def test_databricks_provider_resolves_host_and_refreshes() -> None:
    """The provider reads the host once and re-mints past the throttle window.

    Within the throttle window ``resolve()`` returns the cached token
    (one ``authenticate()`` call); once the window elapses it re-mints,
    which is what keeps a long session alive across OAuth expiry.
    """
    fake = _FakeConfig("https://ws.cloud.databricks.com/", ["tok-1", "tok-2"])
    clock = {"now": 0.0}
    provider = DatabricksProfileTokenProvider(
        "prof",
        refresh_interval=100.0,
        config_factory=lambda _p: fake,
        clock=lambda: clock["now"],
    )
    assert provider.hostname == "ws.cloud.databricks.com"
    assert provider.workspace_url == "https://ws.cloud.databricks.com"

    assert provider.resolve() == "tok-1"
    clock["now"] = 50.0  # still inside the window -> cached, no re-mint
    assert provider.resolve() == "tok-1"
    assert fake.authenticate_calls == 1
    clock["now"] = 150.0  # past the window -> re-mint
    assert provider.resolve() == "tok-2"
    assert fake.authenticate_calls == 2


def test_databricks_provider_requires_host() -> None:
    """A profile without a ``host`` fails loud rather than binding nothing."""
    with pytest.raises(OmnigentError, match="no 'host'"):
        DatabricksProfileTokenProvider("prof", config_factory=lambda _p: _FakeConfig("", []))


def test_databricks_provider_missing_sdk_fails_loud() -> None:
    """The default factory raises a clear error when the SDK is absent."""
    try:
        import databricks.sdk.config  # noqa: F401
    except ImportError:
        with pytest.raises(OmnigentError, match="requires the Databricks SDK"):
            DatabricksProfileTokenProvider("prof")
    else:
        pytest.skip("databricks SDK is installed; missing-SDK path not exercisable")


def test_databricks_runtime_materializes_placeholder_cfg() -> None:
    """Two profiles produce two host-bound rules + a placeholder cfg file.

    The materialized ``.databrickscfg`` must carry only ``oa_cred_*``
    placeholders (never a live token) and name each profile's real host,
    and the rewrites bind per host with a refreshing provider.
    """
    spec = DatabricksProxySpec(
        profiles=[
            DatabricksProfileBinding(profile="wsA"),
            DatabricksProfileBinding(profile="wsB"),
        ],
        default="wsA",
    )
    hosts = {
        "wsA": "https://a.cloud.databricks.com",
        "wsB": "https://b.cloud.databricks.com",
    }

    def factory(profile: str) -> DatabricksProfileTokenProvider:
        return DatabricksProfileTokenProvider(
            profile,
            config_factory=lambda _p, h=hosts[profile]: _FakeConfig(h, [f"live-{profile}"]),
        )

    runtime = CredentialProxyRuntime()
    _prepare_databricks_runtime(spec, runtime, provider_factory=factory)

    assert {r.host for r in runtime.rewrites} == {
        "a.cloud.databricks.com",
        "b.cloud.databricks.com",
    }
    for rule in runtime.rewrites:
        assert rule.scheme == "bearer"
        assert rule.synthetic and rule.synthetic.startswith(SYNTHETIC_CREDENTIAL_PREFIX)
        # The rule resolves the live token lazily via the provider.
        assert rule.resolve_secret().startswith("live-")

    assert runtime.helper_env_updates["DATABRICKS_CONFIG_PROFILE"] == "wsA"

    assert len(runtime.sandbox_files) == 1
    cfg = runtime.sandbox_files[0]
    assert cfg.env_var == "DATABRICKS_CONFIG_FILE"
    assert cfg.mode == 0o600
    assert "[wsA]" in cfg.content and "[wsB]" in cfg.content
    assert "host = https://a.cloud.databricks.com" in cfg.content
    # Placeholders only — no live token ever lands in the sandbox file.
    assert "live-wsA" not in cfg.content and "live-wsB" not in cfg.content
    assert cfg.content.count(SYNTHETIC_CREDENTIAL_PREFIX) == 2


def test_databricks_runtime_rejects_same_host_profiles() -> None:
    """Two profiles on the same workspace host collide in the host-keyed table."""
    spec = DatabricksProxySpec(
        profiles=[
            DatabricksProfileBinding(profile="p1"),
            DatabricksProfileBinding(profile="p2"),
        ]
    )

    def factory(profile: str) -> DatabricksProfileTokenProvider:
        return DatabricksProfileTokenProvider(
            profile,
            config_factory=lambda _p: _FakeConfig("https://same.cloud.databricks.com", ["t"]),
        )

    with pytest.raises(OmnigentError, match="resolve to workspace host"):
        _prepare_databricks_runtime(spec, CredentialProxyRuntime(), provider_factory=factory)


def test_refresh_interval_reresolves_rotated_source(tmp_path: Path) -> None:
    """A source with ``refresh_interval`` re-reads a value that rotates mid-run.

    This is the whole point of the feature: our GitHub App token source is a
    ``command`` that force-mints a fresh ~1h token, so a session running past
    expiry must see the *new* value on a later swap, not the one baked in at
    helper start. Modeled with a ``file`` source rotated between swaps.
    ``refresh_interval=0`` re-resolves on every swap. This assertion fails on
    unpatched main, where the value is resolved once and frozen (and where the
    ``refresh_interval`` field does not exist).
    """
    secret_file = tmp_path / "token.txt"
    secret_file.write_text("token-v1\n", encoding="utf-8")
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "api.example.com",
                env_var="API_TOKEN",
                source=CredentialSourceSpec(
                    kind="file", path=str(secret_file), refresh_interval=0.0
                ),
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    rule = runtime.rewrites[0]
    # No static secret is baked in for a refreshing source; the proxy pulls
    # the current value through the provider on each swap.
    assert rule.real_secret is None
    assert rule.resolve_secret() == "token-v1"

    # Rotate the backing source mid-session (as a fresh App-token mint would).
    secret_file.write_text("token-v2\n", encoding="utf-8")
    assert rule.resolve_secret() == "token-v2"


def test_no_refresh_interval_bakes_in_source(tmp_path: Path) -> None:
    """Without ``refresh_interval`` the secret is resolved once and frozen.

    The control for :func:`test_refresh_interval_reresolves_rotated_source`:
    the default (static) path must keep its baked-in value even after the
    backing source changes, so refreshing is strictly opt-in.
    """
    secret_file = tmp_path / "token.txt"
    secret_file.write_text("token-v1\n", encoding="utf-8")
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "api.example.com",
                env_var="API_TOKEN",
                source=CredentialSourceSpec(kind="file", path=str(secret_file)),
            )
        ]
    )
    runtime = prepare_credential_proxy_runtime(spec, parent_env={})
    rule = runtime.rewrites[0]
    assert rule.real_secret == "token-v1"
    assert rule.secret_provider is None

    secret_file.write_text("token-v2\n", encoding="utf-8")
    # Static source: the rotated value is intentionally NOT picked up.
    assert rule.resolve_secret() == "token-v1"


def test_refreshing_provider_throttles_re_resolution() -> None:
    """The provider caches within the window and re-resolves once past it.

    Mirrors ``test_databricks_provider_resolves_host_and_refreshes``: uses a
    clock seam and a resolver seam so no real subprocess/file is needed. The
    eager resolve at construction consumes the first value; subsequent calls
    inside the window reuse it, and one call past the window re-resolves.
    """
    values = iter(["v1", "v2"])
    resolver_calls = {"n": 0}

    def resolver(_source: CredentialSourceSpec) -> str:
        resolver_calls["n"] += 1
        return next(values)

    clock = {"now": 0.0}
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="command", command="unused"),
        parent_env={},
        refresh_interval=100.0,
        clock=lambda: clock["now"],
        resolver=resolver,
    )
    # Eager resolve at construction already fetched v1.
    assert resolver_calls["n"] == 1
    assert provider.resolve() == "v1"
    clock["now"] = 50.0  # still inside the window -> cached
    assert provider.resolve() == "v1"
    assert resolver_calls["n"] == 1
    clock["now"] = 150.0  # past the window -> re-resolve
    assert provider.resolve() == "v2"
    assert resolver_calls["n"] == 2


def test_refreshing_source_fails_loud_eagerly() -> None:
    """A misconfigured refreshing source raises at prepare time, not first swap.

    Fail-loud parity with the static path: a refreshing ``env`` source whose
    variable is missing must raise during
    :func:`prepare_credential_proxy_runtime`, so a bad config never yields a
    live rule that only blows up on the first outbound request.
    """
    spec = CredentialProxySpec(
        entries=[
            _bearer_entry(
                "h.example.com",
                env_var="T",
                source=CredentialSourceSpec(kind="env", env="MISSING", refresh_interval=0.0),
            )
        ]
    )
    with pytest.raises(ValueError, match="missing or empty"):
        prepare_credential_proxy_runtime(spec, parent_env={})


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_refreshing_provider_rejects_non_finite_or_negative_interval(bad: float) -> None:
    """The provider fails loud on a negative / NaN / infinite interval.

    This is the prepare-time boundary for callers that build a
    :class:`CredentialSourceSpec` directly (bypassing the parser check):
    ``NaN`` would re-resolve on every swap and infinity would disable
    refresh forever, both silently violating the throttle contract.
    """
    with pytest.raises(ValueError, match="finite, non-negative"):
        RefreshingSecretProvider(
            CredentialSourceSpec(kind="command", command="unused"),
            parent_env={},
            refresh_interval=bad,
            resolver=lambda _s: "unused",
        )


def test_refreshing_provider_snapshots_parent_env() -> None:
    """The provider resolves against the env captured at construction.

    A later mutation of the caller's ``parent_env`` dict must not change
    what an ``env`` source resolves to — the trusted startup environment is
    frozen when the provider is built.
    """
    env = {"OA_SECRET": "v1"}
    provider = RefreshingSecretProvider(
        CredentialSourceSpec(kind="env", env="OA_SECRET", refresh_interval=0.0),
        parent_env=env,
        refresh_interval=0.0,
    )
    assert provider.resolve() == "v1"
    env["OA_SECRET"] = "v2"  # mutate the original dict after construction
    assert provider.resolve() == "v1"
