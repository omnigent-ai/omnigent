from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt

GitHubAppTransport = Callable[[str, str, object | None, str], object]
SecretReader = Callable[[str], str]


class GitHubAuthMode(StrEnum):
    TOKEN = "token"
    APP = "app"


class GitHubAppTokenProvider:
    def __init__(
        self,
        client_id: str,
        private_key: str,
        repo: str,
        transport: GitHubAppTransport | None = None,
        clock: Callable[[], datetime] | None = None,
        signer: Callable[[dict[str, object], str], str] | None = None,
    ) -> None:
        self.client_id = _required(client_id, "GitHub App client ID")
        self.private_key = _required(private_key, "GitHub App private key")
        self.repo = repo
        self.transport = transport or _github_app_request
        self.clock = clock or (lambda: datetime.now(UTC))
        self.signer = signer or _sign_app_jwt

    def installation_token(self) -> str:
        now = int(self.clock().timestamp())
        app_jwt = self.signer(
            {
                "iat": now - 60,
                "exp": now + 540,
                "iss": self.client_id,
            },
            self.private_key,
        )
        installation = self.transport(
            "GET",
            f"/repos/{self.repo}/installation",
            None,
            app_jwt,
        )
        if not isinstance(installation, dict) or not installation.get("id"):
            raise RuntimeError("GitHub App installation response did not include an id")
        credentials = self.transport(
            "POST",
            f"/app/installations/{int(installation['id'])}/access_tokens",
            {},
            app_jwt,
        )
        if not isinstance(credentials, dict):
            raise RuntimeError("GitHub App token response must be an object")
        return _required(str(credentials.get("token") or ""), "GitHub App installation token")


def resolve_github_token(
    auth_mode: str,
    repo: str,
    read_secret: SecretReader,
    token_secret_key: str,
    app_client_id_secret_key: str,
    app_private_key_secret_key: str,
    *,
    app_transport: GitHubAppTransport | None = None,
    warn: Callable[[str], None] | None = None,
) -> str:
    mode = GitHubAuthMode(auth_mode.strip().lower())
    if mode == GitHubAuthMode.TOKEN:
        return _read_required_secret(read_secret, token_secret_key)

    try:
        provider = GitHubAppTokenProvider(
            _read_required_secret(read_secret, app_client_id_secret_key),
            _read_required_secret(read_secret, app_private_key_secret_key),
            repo,
            transport=app_transport,
        )
        return provider.installation_token()
    except Exception as app_error:
        try:
            fallback = _read_required_secret(read_secret, token_secret_key)
        except Exception:
            raise RuntimeError(
                "GitHub App authentication failed and PAT fallback is unavailable"
            ) from app_error
        if warn:
            warn("GitHub App authentication failed; using the configured PAT fallback")
        return fallback


def _read_required_secret(read_secret: SecretReader, key: str) -> str:
    try:
        value = read_secret(key)
    except Exception as exc:
        raise RuntimeError(f"Databricks secret {key!r} is unavailable") from exc
    return _required(value, f"Databricks secret {key!r}")


def _required(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise RuntimeError(f"{name} is empty")
    return stripped


def _sign_app_jwt(claims: dict[str, object], private_key: str) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


def _github_app_request(method: str, path: str, payload: object | None, bearer: str) -> object:
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(
        f"https://api.github.com{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            content = response.read()
    except HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc
    return json.loads(content) if content else None
