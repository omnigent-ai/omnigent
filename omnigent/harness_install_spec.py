"""Import-safe install metadata types for harness plugins."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HarnessInstallSpec:
    """Install + auth metadata for one coding-harness CLI.

    This type intentionally lives outside :mod:`omnigent.onboarding` so
    optional harness plugins can declare setup metadata during entry-point
    discovery without importing the onboarding/provider stack.
    """

    display: str
    binary: str
    package: str | None
    login_args: tuple[str, ...] | None = None
    logout_args: tuple[str, ...] | None = None
    status_args: tuple[str, ...] | None = None
    install_hint: str | None = None
    login_status_key: str | None = None
    auth_hint: str | None = None
    install_command: tuple[str, ...] | None = None


@dataclass(frozen=True)
class SetupStep:
    """One requirement in getting a harness ready to run on a host.

    Serialized into the ``GET /v1/harnesses`` catalog (``setup_steps``) so the
    web UI can render a "set up this agent" checklist that mirrors what
    ``omnigent setup`` walks a user through — one row per requirement, in order.

    :param kind: Machine id for the requirement, ``"install"`` or ``"auth"``.
    :param title: Human row label, agent-framed (e.g. ``"Install Codex"``,
        ``"Sign in to Codex"``).
    :param detail: Optional one-line explanation of what the step means for
        this harness (e.g. "Uses your ChatGPT subscription").
    :param action: How the user resolves it — ``"install"`` (a one-click
        install the server performs), ``"command"`` (a command the user runs on
        the host, in :attr:`command`), or ``"setup"`` (run ``omnigent setup`` —
        the M1 fallback for auth methods the UI can't yet drive, e.g. entering
        an API key or gateway).
    :param command: The command for ``action="command"``/``"setup"`` steps
        (e.g. ``"codex login"``); ``None`` for one-click installs.
    :param status_key: Which readiness sub-state marks this step done, or
        ``None`` when the host can't determine it (the step renders as an
        informational instruction, not a tracked ✓/○). ``"installed"`` →
        done once the binary is present; ``"authed"`` → done once the harness
        reports it's authenticated.
    """

    kind: str
    title: str
    detail: str
    action: str
    command: str | None = None
    status_key: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """JSON-serializable row for the ``/v1/harnesses`` catalog."""
        return {
            "kind": self.kind,
            "title": self.title,
            "detail": self.detail,
            "action": self.action,
            "command": self.command,
            "status_key": self.status_key,
        }
