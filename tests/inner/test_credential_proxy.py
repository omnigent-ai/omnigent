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

from omnigent.inner.credential_proxy import (
    SYNTHETIC_CREDENTIAL_PREFIX,
    prepare_credential_proxy_runtime,
)
from omnigent.inner.datamodel import (
    CredentialProxyEntry,
    CredentialProxySpec,
    CredentialSourceSpec,
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
