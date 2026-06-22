"""Tests for ``omnigent login`` against Databricks-fronted servers.

Covers the two Databricks deployment shapes the login probe detects:

- Databricks Apps (the edge 302s unauthenticated requests to the
  workspace OIDC authorize endpoint), and
- workspace-hosted omnigent (the API proxy answers 401 with a
  ``WWW-Authenticate: Bearer realm="DatabricksRealm"`` challenge),

plus the guard rails: the ``databricks`` extra gate, the
``databricks auth login`` fallback when no cached grant resolves, the
app-rejects-token failure, and non-interference with accounts mode.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from omnigent.cli import cli as cli_group

_APPS_URL = "https://myapp-1234.aws.databricksapps.com"
_WORKSPACE = "https://example.databricks.com"
_APPS_REDIRECT = f"{_WORKSPACE}/oidc/oauth2/v2.0/authorize?client_id=abc&response_type=code"
_WORKSPACE_API_URL = f"{_WORKSPACE}/api/2.0/omnigent"


@dataclass
class _FakeHttpx:
    """Scripted ``httpx.get`` replacement returning real Responses.

    :param responses: Queue of responses returned in call order.
    :param requests: URLs and Authorization headers seen, in order.
    """

    responses: list[httpx.Response]
    requests: list[dict[str, str | None]] = field(default_factory=list)

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        """Pop and return the next scripted response.

        :param url: The requested URL.
        :param kwargs: httpx.get kwargs (headers, timeout).
        :returns: The next scripted :class:`httpx.Response`.
        """
        headers = kwargs.get("headers")
        auth = headers.get("Authorization") if isinstance(headers, dict) else None
        self.requests.append({"url": url, "authorization": auth})
        return self.responses.pop(0)


def _response(
    status: int,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, object] | None = None,
) -> httpx.Response:
    """Build a real httpx.Response for the scripted transport.

    :param status: HTTP status code, e.g. ``302``.
    :param headers: Response headers, e.g. ``{"location": "https://..."}``.
    :param body: Optional JSON body.
    :returns: A real :class:`httpx.Response` (so header/json parsing in
        production runs for real).
    """
    return httpx.Response(
        status,
        headers=headers or {},
        content=json.dumps(body).encode() if body is not None else b"",
        request=httpx.Request("GET", "https://probe.invalid/v1/me"),
    )


@pytest.fixture()
def token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect auth_tokens.json to a temp dir (same seam as test_cli_auth).

    :param tmp_path: Pytest temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The temp directory path.
    """
    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    return tmp_path


def _patch_login_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_httpx: _FakeHttpx,
    sdk_installed: bool = True,
    cached_tokens: list[str | None] | None = None,
) -> list[str]:
    """Patch the login command's collaborators for a scripted run.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param fake_httpx: Scripted httpx.get replacement.
    :param sdk_installed: What ``databricks_sdk_installed()`` reports.
    :param cached_tokens: Successive ``_databricks_workspace_token``
        results, e.g. ``[None, "tok"]`` for "no cached grant, then a
        token after ``databricks auth login`` runs". Defaults to a
        cached token on first try.
    :returns: A list capturing each ``subprocess.run`` argv (the
        ``databricks auth login`` invocations).
    """
    import omnigent.cli as cli_mod

    monkeypatch.setattr(httpx, "get", fake_httpx.get)
    # URL normalization has its own probe + dedicated tests; identity here
    # keeps each login test's scripted response sequence aligned with the
    # login body's own requests.
    monkeypatch.setattr(cli_mod, "_workspace_api_server_url", lambda server: server)
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: sdk_installed,
    )
    tokens = list(cached_tokens if cached_tokens is not None else ["tok-cached"])
    monkeypatch.setattr(
        cli_mod,
        "_databricks_workspace_token",
        lambda workspace_host: tokens.pop(0),
    )

    login_calls: list[str] = []

    @dataclass
    class _Completed:
        returncode: int = 0

    def _fake_run(argv: list[str], **kwargs: object) -> _Completed:
        login_calls.append(" ".join(argv[1:]))  # drop the binary path
        return _Completed()

    monkeypatch.setattr(cli_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(cli_mod.shutil, "which", lambda name: f"/usr/bin/{name}")
    return login_calls


def test_login_apps_redirect_stores_pointer_record(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """An Apps 302-to-OIDC probe logs in via the workspace and stores a record.

    The record (not a bearer) is what later commands resolve to fresh
    workspace tokens — this is the core of the no-profile Apps CUJ.
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(
        responses=[
            _response(302, headers={"location": _APPS_REDIRECT}),
            _response(200, body={"user_id": "alice@example.com"}),
        ]
    )
    _patch_login_env(monkeypatch, fake_httpx=fake)

    result = CliRunner().invoke(cli_group, ["login", _APPS_URL])

    assert result.exit_code == 0, result.output
    # The pointer record names the workspace parsed from the redirect —
    # a miss means the auth chain would fall back to ambient credentials.
    assert load_databricks_workspace_host(_APPS_URL) == _WORKSPACE
    # The verify call carried the minted workspace bearer to the app.
    assert fake.requests[-1]["authorization"] == "Bearer tok-cached"
    assert "alice@example.com" in result.output


def test_login_workspace_hosted_401_uses_url_host(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """A DatabricksRealm 401 (workspace API path) logs in to the URL's host.

    Hosted omnigent lives at ``https://<workspace>/api/2.0/omnigent`` —
    the workspace IS the server host, and the record must key on the full
    server URL (path included).
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(
        responses=[
            _response(
                401,
                headers={"www-authenticate": 'Bearer realm="DatabricksRealm"'},
                body={"error_code": 401, "message": "Credential was not sent"},
            ),
            _response(200, body={"user_id": "alice@example.com"}),
        ]
    )
    _patch_login_env(monkeypatch, fake_httpx=fake)

    result = CliRunner().invoke(cli_group, ["login", _WORKSPACE_API_URL])

    assert result.exit_code == 0, result.output
    # Keyed by the full server URL (with the /api/2.0/omnigent path) and
    # pointing at the workspace host without the path.
    assert load_databricks_workspace_host(_WORKSPACE_API_URL) == _WORKSPACE


def test_login_apps_fails_loud_without_databricks_extra(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """Without databricks-sdk installed, login names the extra to install.

    The Databricks branch is gated on the ``databricks`` extra; a silent
    fallback to the OIDC flow would produce a baffling ticket-endpoint
    error instead.
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(responses=[_response(302, headers={"location": _APPS_REDIRECT})])
    _patch_login_env(monkeypatch, fake_httpx=fake, sdk_installed=False)

    result = CliRunner().invoke(cli_group, ["login", _APPS_URL])

    assert result.exit_code != 0
    # The error must carry the canonical install hint, not a generic failure.
    assert "omnigent[databricks]" in result.output
    assert load_databricks_workspace_host(_APPS_URL) is None


def test_login_runs_databricks_auth_login_when_no_cached_grant(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """No cached host-keyed grant → ``databricks auth login --host <ws>`` runs.

    The login is host-keyed (no ``--profile`` / profile name anywhere);
    after it succeeds the token resolves and the record is stored.
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(
        responses=[
            _response(302, headers={"location": _APPS_REDIRECT}),
            _response(200, body={"user_id": "alice@example.com"}),
        ]
    )
    login_calls = _patch_login_env(
        monkeypatch,
        fake_httpx=fake,
        # First lookup misses (no grant), second (post-login) resolves.
        cached_tokens=[None, "tok-fresh"],
    )

    result = CliRunner().invoke(cli_group, ["login", _APPS_URL])

    assert result.exit_code == 0, result.output
    # Exactly one browser login, host-keyed, with no profile flag — a
    # `--profile` here would recreate the named-profile coupling this
    # flow exists to remove.
    assert login_calls == [f"auth login --host {_WORKSPACE}"]
    assert load_databricks_workspace_host(_APPS_URL) == _WORKSPACE


def test_login_fails_loud_when_app_rejects_workspace_token(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """Workspace login OK but the app 403s → no record, actionable error.

    A user without CAN_USE on the app authenticates at the workspace yet
    can't reach the app; storing the record anyway would make every later
    command fail with the same opaque 403.
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(
        responses=[
            _response(302, headers={"location": _APPS_REDIRECT}),
            _response(403),
        ]
    )
    _patch_login_env(monkeypatch, fake_httpx=fake)

    result = CliRunner().invoke(cli_group, ["login", _APPS_URL])

    assert result.exit_code != 0
    assert "403" in result.output
    assert load_databricks_workspace_host(_APPS_URL) is None


def test_login_accounts_mode_not_hijacked_by_databricks_detection(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """A plain accounts-mode 401 (login_url=/login) still routes to accounts.

    The Databricks 401 branch keys on the DatabricksRealm challenge; an
    omnigent accounts server has no such header and must keep its
    username/password flow.
    """
    import omnigent.cli as cli_mod

    fake = _FakeHttpx(responses=[_response(401, body={"login_url": "/login"})])
    _patch_login_env(monkeypatch, fake_httpx=fake)
    accounts_calls: list[str] = []
    monkeypatch.setattr(cli_mod, "_accounts_login", accounts_calls.append)

    result = CliRunner().invoke(cli_group, ["login", "http://localhost:8000"])

    assert result.exit_code == 0, result.output
    # Routed to the accounts flow, exactly once, for the probed server.
    assert accounts_calls == ["http://localhost:8000"]


def test_login_stale_cached_grant_triggers_fresh_login_and_retry(
    monkeypatch: pytest.MonkeyPatch, token_dir: Path
) -> None:
    """A cached grant the server rejects forces ONE browser login + re-verify.

    The Databricks CLI token cache is host-keyed but not issuer-validated:
    a stale entry can mint a token for a different workspace, which the
    server 302s/403s. Failing outright would strand the user; the fresh
    login replaces the bad cache entry and the retry succeeds.
    """
    from omnigent.cli_auth import load_databricks_workspace_host

    fake = _FakeHttpx(
        responses=[
            _response(302, headers={"location": _APPS_REDIRECT}),
            # First verify: stale cached token bounced by the edge.
            _response(302, headers={"location": _APPS_REDIRECT}),
            # Second verify, after the forced fresh login: accepted.
            _response(200, body={"user_id": "alice@example.com"}),
        ]
    )
    login_calls = _patch_login_env(
        monkeypatch,
        fake_httpx=fake,
        # Cached grant resolves both times (stale first, fresh second).
        cached_tokens=["tok-stale", "tok-fresh"],
    )

    result = CliRunner().invoke(cli_group, ["login", _APPS_URL])

    assert result.exit_code == 0, result.output
    # Exactly one forced re-login — a second rejection must fail loud,
    # not loop the browser flow.
    assert login_calls == [f"auth login --host {_WORKSPACE}"]
    # The retry verify presented the freshly minted token, not the stale one.
    assert fake.requests[-1]["authorization"] == "Bearer tok-fresh"
    assert load_databricks_workspace_host(_APPS_URL) == _WORKSPACE


# ── bare-workspace URL expansion ────────────────────────────────────


def _scripted_normalizer_httpx(
    monkeypatch: pytest.MonkeyPatch,
    responses_by_url: dict[str, httpx.Response],
) -> list[str]:
    """Route ``httpx.get`` by exact URL for normalizer tests.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param responses_by_url: Exact-URL → response map; an unmapped URL
        fails the test loudly (it means an unexpected probe ran).
    :returns: The list of URLs probed, in order.
    """
    probed: list[str] = []

    def _get(url: str, **kwargs: object) -> httpx.Response:
        probed.append(url)
        assert url in responses_by_url, f"unexpected probe: {url}"
        return responses_by_url[url]

    monkeypatch.setattr(httpx, "get", _get)
    return probed


def test_workspace_url_expands_bare_workspace_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare workspace URL expands to its /api/2.0/omnigent mount.

    The bare host serves the workspace web app (404 + ``server:
    databricks`` for /v1/me); the API mount answers with the
    DatabricksRealm challenge. Users paste the bare host, so this is
    the difference between "just works" and a confusing 404.
    """
    import omnigent.cli as cli_mod

    _scripted_normalizer_httpx(
        monkeypatch,
        {
            f"{_WORKSPACE}/v1/me": _response(404, headers={"server": "databricks"}),
            f"{_WORKSPACE_API_URL}/v1/me": _response(
                401,
                headers={"www-authenticate": 'Bearer realm="DatabricksRealm"'},
            ),
        },
    )

    assert cli_mod._workspace_api_server_url(_WORKSPACE) == _WORKSPACE_API_URL


def test_workspace_url_leaves_oss_server_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path-less OSS server (no databricks header) is never rewritten.

    Rewriting here would break every self-hosted deployment whose
    server happens to 401 the unauthenticated probe.
    """
    import omnigent.cli as cli_mod

    _scripted_normalizer_httpx(
        monkeypatch,
        {
            "https://omni.example.com/v1/me": _response(401, body={"login_url": "/login"}),
        },
    )

    assert (
        cli_mod._workspace_api_server_url("https://omni.example.com") == "https://omni.example.com"
    )


def test_workspace_url_leaves_apps_edge_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks Apps URL (302 to OIDC) is already a server base.

    Apps hosts are path-less AND answer with ``server: databricks`` —
    only the login-target detection distinguishes them from a bare
    workspace, so a regression here would bolt /api/2.0/omnigent onto
    every app URL.
    """
    import omnigent.cli as cli_mod

    _scripted_normalizer_httpx(
        monkeypatch,
        {
            f"{_APPS_URL}/v1/me": _response(
                302, headers={"location": _APPS_REDIRECT, "server": "databricks"}
            ),
        },
    )

    assert cli_mod._workspace_api_server_url(_APPS_URL) == _APPS_URL


def test_workspace_url_with_path_probes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URLs that already carry a path return untouched with zero probes.

    The expansion is a convenience for pasted bare hosts only — an
    explicit path is the user's choice, and probing on every command
    would tax all remote invocations.
    """
    import omnigent.cli as cli_mod

    probed = _scripted_normalizer_httpx(monkeypatch, {})

    result = cli_mod._workspace_api_server_url(f"{_WORKSPACE_API_URL}/")

    assert result == _WORKSPACE_API_URL
    assert probed == []


def test_workspace_url_expands_when_mount_hidden_from_anonymous_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mount invisible to anonymous probes expands via a cached bearer.

    Azure workspace edges answer the anonymous /api/2.0/omnigent probe
    with a plain 404 — not the AWS proxy's 401-with-DatabricksRealm
    challenge — so a mount that works for authenticated callers looks
    absent and the user gets stranded on the bare workspace URL. With
    a cached ``databricks auth login`` grant, the probe is retried
    authenticated and the mount is adopted.
    """
    import omnigent.cli as cli_mod

    fake = _FakeHttpx(
        responses=[
            # Root: the workspace web app shape that triggers expansion.
            _response(404, headers={"server": "databricks"}),
            # Mount, anonymous: hidden — plain 404, no realm challenge.
            _response(404, headers={"server": "databricks"}),
            # Mount, authenticated: omnigent answers.
            _response(200, body={"user_id": "alice"}),
        ]
    )
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: True,
    )
    minted_for: list[str] = []

    def _token(workspace_host: str) -> str:
        minted_for.append(workspace_host)
        return "tok-ws"

    monkeypatch.setattr(cli_mod, "_databricks_workspace_token", _token)

    result = cli_mod._workspace_api_server_url(_WORKSPACE)

    assert result == _WORKSPACE_API_URL
    # The bearer is minted for the workspace host (the bare URL) — that
    # is what the Databricks OAuth token cache is keyed on, not the
    # /api/2.0/omnigent candidate.
    assert minted_for == [_WORKSPACE]
    # Probe order: anonymous root, anonymous mount, authenticated mount.
    # A missing third request means the authed retry never ran; a
    # Bearer on either anonymous probe would present credentials to an
    # endpoint not yet known to be Databricks-fronted.
    assert [r["url"] for r in fake.requests] == [
        f"{_WORKSPACE}/v1/me",
        f"{_WORKSPACE_API_URL}/v1/me",
        f"{_WORKSPACE_API_URL}/v1/me",
    ]
    assert [r["authorization"] for r in fake.requests] == [None, None, "Bearer tok-ws"]


def test_workspace_url_hints_when_mount_dark_and_no_cached_grant(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Declining a workspace-shaped URL explains itself and the remedy.

    Without a cached grant there is nothing to retry with, so the URL
    is left as given — but a silent decline strands the user on a bare
    workspace URL whose host tunnel can only 404, so the decline must
    name the ``databricks auth login`` remedy.
    """
    import omnigent.cli as cli_mod

    fake = _FakeHttpx(
        responses=[
            _response(404, headers={"server": "databricks"}),
            _response(404, headers={"server": "databricks"}),
        ]
    )
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: True,
    )
    monkeypatch.setattr(cli_mod, "_databricks_workspace_token", lambda workspace_host: None)

    result = cli_mod._workspace_api_server_url(_WORKSPACE)

    assert result == _WORKSPACE
    # No grant resolves → no authenticated retry: exactly the two
    # anonymous probes ran. A third request here would mean a Bearer
    # header was fabricated from nothing.
    assert [r["authorization"] for r in fake.requests] == [None, None]
    out = capsys.readouterr().out
    # The hint must name the remedy, otherwise the user is back to
    # debugging an opaque host-tunnel 404 on the bare workspace URL.
    assert f"databricks auth login --host {_WORKSPACE}" in out


def test_workspace_url_hints_when_authed_probe_also_misses(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A mount dark even to authenticated probes declines with a hint.

    A 404 with valid credentials means omnigent is genuinely not
    served on this workspace (or the grant is stale) — the URL is left
    as given, and the message distinguishes this from the
    never-logged-in case so the user doesn't loop on `auth login`.
    """
    import omnigent.cli as cli_mod

    fake = _FakeHttpx(
        responses=[
            _response(404, headers={"server": "databricks"}),
            _response(404, headers={"server": "databricks"}),
            # Mount, authenticated: still 404 — not served here.
            _response(404, headers={"server": "databricks"}),
        ]
    )
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: True,
    )
    monkeypatch.setattr(cli_mod, "_databricks_workspace_token", lambda workspace_host: "tok-ws")

    result = cli_mod._workspace_api_server_url(_WORKSPACE)

    assert result == _WORKSPACE
    # The authed retry ran and presented the cached bearer; without it
    # this test would be identical to the no-grant case.
    assert [r["authorization"] for r in fake.requests] == [None, None, "Bearer tok-ws"]
    out = capsys.readouterr().out
    # The message must say credentials were already tried — pointing
    # the user at `auth login` alone would send them in a circle.
    assert "even with the cached workspace credentials" in out


def test_workspace_url_skips_authed_probe_without_databricks_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``databricks`` extra → the token cache is never consulted.

    ``_databricks_workspace_token`` imports the databricks-sdk-backed
    auth resolver; calling it without the extra installed raises
    ModuleNotFoundError. The ``databricks_sdk_installed()`` gate must
    short-circuit first so plain OSS installs keep working.
    """
    import omnigent.cli as cli_mod

    fake = _FakeHttpx(
        responses=[
            _response(404, headers={"server": "databricks"}),
            _response(404, headers={"server": "databricks"}),
        ]
    )
    monkeypatch.setattr(httpx, "get", fake.get)
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: False,
    )

    def _must_not_mint(workspace_host: str) -> str:
        raise AssertionError(
            "_databricks_workspace_token was called without the databricks "
            "extra installed — the databricks_sdk_installed() gate in "
            "_cached_workspace_bearer did not short-circuit."
        )

    monkeypatch.setattr(cli_mod, "_databricks_workspace_token", _must_not_mint)

    result = cli_mod._workspace_api_server_url(_WORKSPACE)

    assert result == _WORKSPACE
    # Only the two anonymous probes — no token, no authed retry.
    assert [r["authorization"] for r in fake.requests] == [None, None]
