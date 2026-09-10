from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ConfigError(Exception):
    """A configuration problem stated in operator-friendly terms.

    Raised by :func:`load_settings` instead of surfacing a raw pydantic
    ``ValidationError`` (internal field names + a traceback). The message is
    safe and useful to print straight to a terminal.
    """


def _is_loopback_url(url: str) -> bool:
    """Whether ``url``'s host is loopback (localhost / 127.0.0.1 / ::1).

    Used to allow a plaintext ``http://`` server URL for local testing only,
    while requiring https for any real host.
    """
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _local_data_dir() -> Path:
    """Return the local runtime data dir for the bot's SQLite store.

    Honors ``OMNIGENT_DATA_DIR`` (the shared data-isolation knob, so a
    checkout/worktree keeps its own state), else ``~/.omnigent``. Kept as a
    local copy rather than an import so the standalone ``omnigent-discord``
    package stays decoupled from omnigent core.

    :returns: The data directory path (callers create it lazily).
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".omnigent"


def _split_ids(raw: str | None) -> frozenset[str]:
    """Parse a comma/space-separated snowflake list into a set of ids."""
    if not raw:
        return frozenset()
    return frozenset(part for part in raw.replace(",", " ").split() if part)


class Settings(BaseSettings):
    # Config is read from real environment variables only — no ``env_file``.
    # This mirrors ``omni server`` / the core CLI, which load no ``.env``:
    # whatever populates the environment (shell, ``uv run``, the container
    # deploy) is the single source of truth. For local dev, export the vars or
    # run under a tool that injects them (e.g. ``uv run --env-file .env``).
    # See integrations/discord/README.
    model_config = SettingsConfigDict(
        extra="ignore",
        case_sensitive=False,
    )

    # The bot token from the Discord developer portal (Bot → Token). Discord
    # has no second app-level token: one bot token opens the gateway websocket
    # AND authorizes every REST call, so unlike Slack there is only one.
    bot_token: str = Field(validation_alias="OMNIGENT_DISCORD_BOT_TOKEN")

    # The one Omnigent server this bot talks to. Set by the operator, never by
    # a Discord user — so the bot only ever issues requests to this fixed host
    # (closes the SSRF vector a user-supplied URL would open). Every user still
    # authenticates as their own identity against it.
    server_url: str = Field(validation_alias="OMNIGENT_SERVER_URL")

    # Optional allow-list of guild (server) snowflakes the bot will act in. A
    # Discord bot invite link can be used by anyone with Manage Server on any
    # guild, so — unlike a Slack app installed by a workspace admin — an
    # unrestricted bot can be dragged into a guild the operator never approved.
    # When set, messages from any other guild are ignored outright. Empty means
    # "act anywhere the bot has been added".
    # ``NoDecode`` because pydantic-settings would otherwise JSON-parse a
    # collection-typed variable, and ``900,901`` is not JSON. The validator
    # below sees the raw string instead.
    allowed_guild_ids: Annotated[frozenset[str], NoDecode] = Field(
        default_factory=frozenset,
        validation_alias="OMNIGENT_DISCORD_GUILD_IDS",
    )

    # OPT-IN fast-iteration override. Registering ``/omnigent`` into a guild
    # makes it appear in seconds, but a guild command is DM-blind: Discord
    # routes DM interactions only to globally-registered commands, so setting
    # this removes ``/omnigent`` from DMs — including ``/omnigent new``, the
    # only way to end a DM session. Left empty (the default) the command is
    # published globally and works in guilds and DMs alike. Deliberately NOT
    # defaulted from the allow-list, which is a security control with an
    # unrelated purpose.
    command_guild_ids: Annotated[frozenset[str], NoDecode] = Field(
        default_factory=frozenset,
        validation_alias="OMNIGENT_DISCORD_COMMAND_GUILD_IDS",
    )

    # OPERATOR gate for reading attached files, off by default. Fetching a file
    # an arbitrary Discord user attached is the only place this bot pulls
    # unbounded bytes from the network and hands them to an agent, so it stays
    # closed until someone running the bot opens it. A user must ALSO opt in via
    # ``/omnigent config``; either side being off means no file is sent.
    allow_file_upload: bool = Field(
        default=False,
        validation_alias="OMNIGENT_DISCORD_ALLOW_FILE_UPLOAD",
    )

    # Per-file ceiling. Discord itself caps uploads well above this; the point
    # here is to bound what one message can push through the server, not to
    # mirror Discord's limit.
    max_attachment_bytes: int = Field(
        default=8 * 1024 * 1024,
        validation_alias="OMNIGENT_DISCORD_MAX_ATTACHMENT_BYTES",
    )

    # Per-message ceiling, so one message cannot queue an unbounded number of
    # downloads.
    max_attachments_per_message: int = Field(
        default=5,
        validation_alias="OMNIGENT_DISCORD_MAX_ATTACHMENTS",
    )

    # Optional shared secret proving this bot is an authorized device-grant
    # client. When the Omnigent server has OMNIGENT_DEVICE_CLIENT_SECRET set,
    # this must match; the bot sends it in the X-Omnigent-Client-Secret header
    # on device authorize/token/revoke. Leave unset when the server doesn't
    # require it.
    device_client_secret: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_DEVICE_CLIENT_SECRET",
    )

    # Bot SQLite store (channel→session map, user configs, encrypted tokens).
    # Defaults under the runtime data dir (``OMNIGENT_DATA_DIR`` or
    # ``~/.omnigent``) so the daemon doesn't depend on its launch cwd — set
    # OMNIGENT_DISCORD_DATABASE_PATH to override.
    database_path: Path = Field(
        default_factory=lambda: _local_data_dir() / "omnigent_discord.sqlite3",
        validation_alias="OMNIGENT_DISCORD_DATABASE_PATH",
    )
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")

    # Fernet key (urlsafe-base64, 32 bytes) that encrypts the delegated
    # Omnigent access/refresh tokens at rest in the local SQLite store.
    # Generate with ``python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"``. Set this so a stolen database
    # file cannot be used to impersonate users — see designs/DEVICE_AUTH.md. If
    # unset, tokens are kept in memory only (never written to disk) and lost on
    # restart, so users re-authenticate; the integration still works either way.
    token_encryption_key: str | None = Field(
        default=None,
        validation_alias="OMNIGENT_DISCORD_TOKEN_ENCRYPTION_KEY",
    )

    # Seconds between edits of the live streaming reply. Discord has no
    # streaming-message API, so a reply is one message edited in place; edits
    # are rate-limited per channel, and a too-eager cadence burns the bucket and
    # stalls the whole turn. ~1s reads as live while staying well inside the
    # limit. Raise it on a busy bot, lower it only for local experiments.
    stream_edit_interval_seconds: float = Field(
        default=1.0,
        ge=0.25,
        le=10.0,
        validation_alias="OMNIGENT_DISCORD_STREAM_EDIT_INTERVAL",
    )

    @field_validator("allowed_guild_ids", "command_guild_ids", mode="before")
    @classmethod
    def _parse_guild_ids(cls, value: object) -> object:
        return _split_ids(value) if isinstance(value, str) else value

    @field_validator("server_url")
    @classmethod
    def _normalize_server_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("OMNIGENT_SERVER_URL must start with http:// or https://")
        # The per-user delegated bearer is sent to this host on every request
        # (see omnigent.py). Plaintext http:// would transmit that credential in
        # the clear, so reject it for any non-loopback host. Loopback stays
        # allowed for local dev.
        if value.startswith("http://") and not _is_loopback_url(value):
            raise ValueError(
                "OMNIGENT_SERVER_URL must use https:// (plaintext would leak the "
                "delegated bearer token); http:// is allowed only for loopback"
            )
        return value

    def guild_allowed(self, guild_id: str | None) -> bool:
        """Whether the bot may act in ``guild_id``.

        ``None`` is a DM, which carries no guild and is always allowed — a DM
        reaches the bot only from a user who shares a guild with it, and the
        per-user setup gate still applies.
        """
        if guild_id is None or not self.allowed_guild_ids:
            return True
        return guild_id in self.allowed_guild_ids


# Required env vars → a short human label, so a missing-config error can name
# exactly what to set. Only the fields with no default are truly required.
_REQUIRED_ENV_VARS: dict[str, str] = {
    "OMNIGENT_DISCORD_BOT_TOKEN": "Discord bot token (Developer Portal → Bot)",
    "OMNIGENT_SERVER_URL": "Omnigent server URL (https://…)",
}


def load_settings() -> Settings:
    """Load settings from the environment, with an operator-friendly error.

    A missing/invalid config raises :class:`ConfigError` carrying a message fit
    to print directly — naming the missing environment variables and how to set
    them — instead of a raw pydantic ``ValidationError`` traceback. Config is
    read from real environment variables only (no ``.env`` loading); see the
    module comments / integrations/discord/README.
    """
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        # Separate the two failure kinds so the message is precise: a required
        # var not set at all (pydantic "missing") vs. a value that failed a
        # validator (bad URL, out-of-range interval, …).
        missing: list[str] = []
        invalid: list[str] = []
        for err in exc.errors():
            field = str(err["loc"][0]) if err["loc"] else ""
            env_name = _env_alias_for(field)
            if err["type"] == "missing":
                missing.append(env_name)
            else:
                invalid.append(f"{env_name}: {err['msg']}")

        lines: list[str] = []
        if missing:
            lines.append("Missing required configuration. Set these environment variables:")
            for name in missing:
                label = _REQUIRED_ENV_VARS.get(name, "")
                lines.append(f"  • {name}" + (f"  — {label}" if label else ""))
        if invalid:
            if lines:
                lines.append("")
            lines.append("Invalid configuration:")
            lines.extend(f"  • {item}" for item in invalid)
        lines.append(
            "\nThe bot reads config from the environment (it does NOT load a .env "
            "file itself). Export the variables, or launch under a tool that "
            "injects them — e.g. `uv run --env-file .env omni integration discord`. "
            "See integrations/discord/.env.example for the full set."
        )
        raise ConfigError("\n".join(lines)) from exc


def _env_alias_for(field_name: str) -> str:
    """Return the env-var alias for a Settings field (fallback: the field name).

    The friendly error names the environment variable the operator sets (e.g.
    ``OMNIGENT_SERVER_URL``), not the internal snake_case field.
    """
    info = Settings.model_fields.get(field_name)
    alias = getattr(info, "validation_alias", None) if info is not None else None
    return alias if isinstance(alias, str) else field_name.upper()
