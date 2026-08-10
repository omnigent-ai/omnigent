"""``create_app`` fails closed on an invalid ``manager_webhook`` config (OMN-104).

``manager_webhook_dispatcher._run_once`` re-checks the same config every idle
cycle and, on ``ManagerWebhookConfigError``, logs a warning and treats the
webhook as disabled rather than crashing an already-running server — a config
file edited into a bad state after boot must not take the server down. But an
operator who *starts* the server with ``manager_webhook.enabled: true`` and a
bad endpoint deserves an immediate, loud startup failure instead of a server
that appears to boot fine and then silently never delivers anything. This
file locks in that boot-time gate directly on ``create_app``, independent of
``manager_webhook_config()``'s own unit tests in
``test_manager_webhook_signing.py`` (which cover the validation rules, not
where in the app lifecycle they're enforced).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.server_config import ManagerWebhookConfigError
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


def _write_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, object]
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data))
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config_path))


def _build_app(db_uri: str, tmp_path: Path) -> object:
    """Construct create_app with the minimal required stores, nothing else."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )


def test_create_app_raises_on_enabled_without_endpoint(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``manager_webhook.enabled: true`` with no endpoint must fail app
    construction, not silently boot into a dispatcher that idles forever."""
    _write_config(tmp_path, monkeypatch, {"manager_webhook": {"enabled": True}})
    with pytest.raises(ManagerWebhookConfigError):
        _build_app(db_uri, tmp_path)


def test_create_app_raises_on_enabled_with_http_endpoint(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-HTTPS endpoint without the explicit dev override must fail
    app construction the same way ``manager_webhook_config()`` itself does."""
    _write_config(
        tmp_path,
        monkeypatch,
        {"manager_webhook": {"enabled": True, "endpoint": "http://manager.example.com/hook"}},
    )
    with pytest.raises(ManagerWebhookConfigError):
        _build_app(db_uri, tmp_path)


def test_create_app_boots_with_manager_webhook_disabled(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (no config block at all) must not be affected by the gate."""
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _build_app(db_uri, tmp_path)


def test_create_app_boots_with_valid_enabled_config(
    db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A correctly configured, enabled webhook must not trip the gate."""
    _write_config(
        tmp_path,
        monkeypatch,
        {"manager_webhook": {"enabled": True, "endpoint": "https://manager.example.com/hook"}},
    )
    _build_app(db_uri, tmp_path)
