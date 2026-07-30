"""Hermes readiness + config reporting for ``omnigent setup``.

Like :mod:`omnigent.onboarding.goose_auth`, Omnigent manages **no** Hermes
credentials: Hermes owns its own auth via ``hermes model`` (an interactive
provider/model picker) which writes the chosen provider + model into
``~/.hermes/config.yaml``. This module is a thin, read-only reporter — it
confirms the ``hermes`` binary is installed and surfaces the configured
provider/model so ``omnigent setup`` can show Hermes as ready (and which model
it will drive) instead of always reading "Not configured" on an installed
binary.

Detection reads ``~/.hermes/config.yaml`` directly — the same user config the
native bridge copies forward in
:func:`omnigent.hermes_native_bridge._load_user_hermes_config`. A fresh install
ships ``model.provider: auto`` (auto-detect from credentials — nothing picked
yet); a finished ``hermes model`` run replaces that with a concrete provider id
(e.g. ``openrouter``). So "configured" is a concrete, non-``auto`` provider,
which cleanly distinguishes a completed ``hermes model`` run from an untouched
scaffold. As with Goose, a bad/absent credential surfaces at run time via
Hermes' own error — the daemon's launch gate (:mod:`harness_readiness`)
deliberately fails open — so this reporter gates only on the picked provider,
never on credential resolution it cannot reliably enumerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import HERMES_KEY, harness_cli_installed

#: Provider value Hermes ships in a fresh ``config.yaml``. ``"auto"`` means
#: "auto-detect from credentials", i.e. the user has not picked a provider via
#: ``hermes model`` yet — treated as not-configured for the setup overview.
_AUTO_PROVIDER = "auto"


def hermes_cli_installed() -> bool:
    """Return whether the ``hermes`` binary is on ``PATH``."""
    return harness_cli_installed(HERMES_KEY)


def hermes_config_path() -> Path:
    """Return the user's Hermes config path (``~/.hermes/config.yaml``)."""
    return Path.home() / ".hermes" / "config.yaml"


@dataclass(frozen=True)
class HermesConfigSummary:
    """What setup needs to know about the local Hermes configuration.

    :param installed: ``hermes`` binary present on ``PATH``.
    :param provider: Configured ``model.provider`` (a concrete provider id),
        or ``None`` when unset, empty, or still the ``auto`` scaffold default.
    :param model: Configured ``model.default`` model id, or ``None``.
    """

    installed: bool
    provider: str | None
    model: str | None

    @property
    def ready(self) -> bool:
        """Configured once a concrete provider has been picked via ``hermes model``.

        A fresh install ships ``provider: auto`` (nothing chosen); a finished
        ``hermes model`` run writes a concrete provider id. Gate on the binary
        too so an absent CLI never reads as ready.
        """
        return self.installed and self.provider is not None

    def describe(self) -> str:
        """Return a one-line status for the setup overview.

        e.g. ``"openrouter / z-ai/glm-5.2"`` when both are known, else whichever
        of provider/model is set, falling back to ``"Configured"``.
        """
        if self.provider and self.model:
            return f"{self.provider} / {self.model}"
        return self.provider or self.model or "Configured"


def _model_section() -> dict:
    """Return the ``model`` mapping from ``~/.hermes/config.yaml`` (best-effort).

    Returns ``{}`` on a missing file, parse failure, or a non-mapping top-level
    document / ``model`` value — this is read-only reporting and must never
    raise.
    """
    path = hermes_config_path()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(data, dict):
        return {}
    model = data.get("model")
    return model if isinstance(model, dict) else {}


def hermes_config_summary() -> HermesConfigSummary:
    """Summarize the local Hermes configuration for the setup overview.

    Reads ``model.provider`` (reported as ``None`` when unset, empty, or the
    ``auto`` scaffold default) and the selected model id (``model.default``,
    accepting the ``model.model`` alternate spelling Hermes also honors).
    """
    section = _model_section()
    raw_provider = section.get("provider")
    provider = raw_provider.strip() if isinstance(raw_provider, str) else ""
    if provider.lower() == _AUTO_PROVIDER:
        provider = ""
    raw_model = section.get("default") or section.get("model")
    model = raw_model.strip() if isinstance(raw_model, str) else ""
    return HermesConfigSummary(
        installed=hermes_cli_installed(),
        provider=provider or None,
        model=model or None,
    )
