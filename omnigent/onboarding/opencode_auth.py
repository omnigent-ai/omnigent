"""OpenCode readiness + credential reporting for ``omnigent setup``.

Omnigent stores **no** OpenCode credentials: OpenCode owns provider auth via
``opencode auth login`` or ambient provider env vars. OpenCode 2.x keeps
credentials in the ``credential`` table of its SQLite DB
(``~/.local/share/opencode/opencode.db``) and imports a legacy ``auth.json``
once. This module reads both, read-only and best-effort, so setup can report
which providers are reachable and the runner can seed per-session data dirs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import OPENCODE_KEY, harness_cli_installed

# Common OpenCode providers → (provider id, display label, env var). The
# provider id matches OpenCode's own id (the ``auth.json`` key and the
# ``provider/model`` prefix in ``opencode models``). Not exhaustive (OpenCode
# resolves many providers from models.dev); this is the set worth surfacing in
# setup, including the ``OPENAI_*`` pair the Databricks-gateway path uses.
_ENV_PROVIDER_VARS: tuple[tuple[str, str, str], ...] = (
    ("openai", "OpenAI", "OPENAI_API_KEY"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY"),
    ("google", "Google Gemini", "GEMINI_API_KEY"),
    ("google", "Google Gemini", "GOOGLE_GENERATIVE_AI_API_KEY"),
    ("groq", "Groq", "GROQ_API_KEY"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
    ("xai", "xAI", "XAI_API_KEY"),
    ("mistral", "Mistral", "MISTRAL_API_KEY"),
    ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY"),
)


def opencode_data_dir() -> Path:
    """Return OpenCode's data dir (``Global.Path.data``), honoring ``XDG_DATA_HOME``."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def opencode_auth_path() -> Path:
    """Return OpenCode's legacy ``auth.json`` path for this process's HOME."""
    return opencode_data_dir() / "auth.json"


def opencode_db_path() -> Path | None:
    """Return OpenCode 2.x's SQLite DB path (``OPENCODE_DB`` resolved against the data dir).

    :returns: The DB path, or ``None`` for an in-memory DB.
    """
    override = os.environ.get("OPENCODE_DB", "").strip()
    if override == ":memory:":
        return None
    if override:
        path = Path(override)
        return path if path.is_absolute() else opencode_data_dir() / path
    return opencode_data_dir() / "opencode.db"


def _legacy_auth_entry(raw: object) -> dict[str, object] | None:
    """Map a v2 ``Credential.Value`` JSON onto the legacy ``auth.json`` entry shape."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    if value.get("type") == "key" and isinstance(value.get("key"), str):
        entry: dict[str, object] = {"type": "api", "key": value["key"]}
        strings = {k: v for k, v in metadata.items() if isinstance(v, str)}
        if strings:
            entry["metadata"] = strings
        return entry
    if value.get("type") == "oauth":
        refresh, access, expires = value.get("refresh"), value.get("access"), value.get("expires")
        if not (isinstance(refresh, str) and isinstance(access, str) and isinstance(expires, int)):
            return None
        entry = {"type": "oauth", "refresh": refresh, "access": access, "expires": expires}
        if isinstance(metadata.get("accountID"), str):
            entry["accountId"] = metadata["accountID"]
        if isinstance(metadata.get("enterpriseUrl"), str):
            entry["enterpriseUrl"] = metadata["enterpriseUrl"]
        return entry
    return None


def stored_v2_credentials(db_path: Path | None = None) -> dict[str, dict[str, object]]:
    """Return OpenCode 2.x credentials as legacy ``auth.json`` entries.

    Opens the DB read-only; any missing file, missing table or SQLite error yields ``{}``.
    Rows are ordered so the active (then newest) credential per integration wins.

    :param db_path: DB to read; ``None`` uses :func:`opencode_db_path`.
    :returns: ``{integration_id: entry}``.
    """
    path = db_path or opencode_db_path()
    if path is None or not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT integration_id, value FROM credential WHERE integration_id IS NOT NULL"
            " ORDER BY COALESCE(active, 0), time_updated"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    credentials: dict[str, dict[str, object]] = {}
    for integration_id, raw in rows:
        entry = _legacy_auth_entry(raw)
        if entry is not None and isinstance(integration_id, str) and integration_id:
            credentials[integration_id] = entry
    return credentials


def _stored_providers() -> tuple[str, ...]:
    """Return provider ids with stored credentials (``auth.json`` ∪ the v2 DB).

    Best-effort: unreadable sources contribute nothing. An empty ``auth.json``
    value is config shape, not a credential, so it is ignored.
    """
    ids: list[str] = []
    try:
        data = json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        ids.extend(str(k) for k, v in data.items() if bool(v))
    for integration_id in stored_v2_credentials():
        if integration_id not in ids:
            ids.append(integration_id)
    return tuple(ids)


def _env_providers(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    """Return provider labels whose API-key env var is present."""
    env = os.environ if environ is None else environ
    seen: list[str] = []
    for _provider_id, label, var in _ENV_PROVIDER_VARS:
        if env.get(var, "").strip() and label not in seen:
            seen.append(label)
    return tuple(seen)


def reachable_provider_ids(environ: dict[str, str] | None = None) -> frozenset[str]:
    """Return OpenCode provider ids reachable from stored auth + env keys.

    Ids match OpenCode's own (the ``provider/model`` prefix), so callers can
    filter a model list down to what the user can actually authenticate.
    """
    env = os.environ if environ is None else environ
    ids = set(_stored_providers())
    for provider_id, _label, var in _ENV_PROVIDER_VARS:
        if env.get(var, "").strip():
            ids.add(provider_id)
    return frozenset(ids)


@dataclass(frozen=True)
class OpenCodeAuthSummary:
    """What setup needs to know about the local OpenCode credentials.

    :param installed: ``opencode`` binary present on ``PATH``.
    :param stored_providers: Provider ids with credentials in ``auth.json``.
    :param env_providers: Provider labels whose API-key env var is set.
    """

    installed: bool
    stored_providers: tuple[str, ...]
    env_providers: tuple[str, ...]

    @property
    def has_provider(self) -> bool:
        """Whether any provider is reachable (stored credential or env key)."""
        return bool(self.stored_providers or self.env_providers)

    @property
    def ready(self) -> bool:
        """Launchable when the CLI is installed AND a provider is configured."""
        return self.installed and self.has_provider

    def describe(self) -> str:
        """A short human summary of configured providers, e.g.
        ``"2 stored (anthropic, openai) + env: OpenAI"``.
        """
        parts: list[str] = []
        if self.stored_providers:
            parts.append(
                f"{len(self.stored_providers)} stored ({', '.join(sorted(self.stored_providers))})"
            )
        if self.env_providers:
            parts.append(f"env: {', '.join(self.env_providers)}")
        return " · ".join(parts) if parts else "no provider configured yet"


def opencode_auth_summary() -> OpenCodeAuthSummary:
    """Summarize the local OpenCode credential state for setup display."""
    return OpenCodeAuthSummary(
        installed=harness_cli_installed(OPENCODE_KEY),
        stored_providers=_stored_providers(),
        env_providers=_env_providers(),
    )
