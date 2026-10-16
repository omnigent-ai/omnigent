"""The Databricks Apps entry point honors the server config's ``sandbox:`` section.

``omnigent server`` parses ``sandbox:`` and passes the result to ``create_app``
so a deployment can offer managed sandbox hosts (``host_type: "managed"``).
The Databricks Apps entry point built ``create_app()`` without it, so the same
section in ``OMNIGENT_CONFIG`` was silently ignored and every user had to run
``omnigent host`` locally before starting a session.

Boots the real module-level entry point (``deploy/databricks/src/app.py``)
through the cold-start test's harness, with ``create_app`` replaced by a
recording mock, and reads back what reached it.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from omnigent.server.managed_hosts import ManagedSandboxDeployment
from tests.deploy.test_databricks_app_lakebase_cold_start import (  # noqa: F401 — registers the fixture
    _APP_MODULE,
    _patch_migrations,
    _stub_downstream_boot,
    boot_exit_codes,
)

_SANDBOX = {"provider": "modal", "server_url": "https://omnigent.example.com"}


def _boot_with_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config: dict[str, object] | None
) -> MagicMock:
    """Boot the entry point against *config* and return the ``create_app`` mock.

    :param monkeypatch: pytest monkeypatch (env + module patching).
    :param tmp_path: Where the server config file is written.
    :param config: Server config mapping, or ``None`` for no config file.
    :returns: The recording ``create_app`` mock after the boot ran.
    """
    if config is None:
        monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
        monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "no-config"))
    else:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(config))
        monkeypatch.setenv("OMNIGENT_CONFIG", str(path))
    _patch_migrations(monkeypatch, lambda _attempt: None)
    _stub_downstream_boot(monkeypatch)
    import omnigent.server.app as server_app

    create_app = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(server_app, "create_app", create_app)
    importlib.import_module(_APP_MODULE)
    return create_app


def test_sandbox_section_reaches_create_app(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A valid ``sandbox:`` section is parsed and handed to ``create_app``."""
    exit_codes = request.getfixturevalue("boot_exit_codes")
    create_app = _boot_with_config(monkeypatch, tmp_path, {"sandbox": _SANDBOX})

    assert exit_codes == []
    create_app.assert_called_once()
    passed = create_app.call_args.kwargs["sandbox_config"]
    assert isinstance(passed, ManagedSandboxDeployment)
    # Compare the parsed fields: the deployment object is not structurally
    # comparable, and the fields are what the launch path reads.
    (config,) = passed.configs
    assert (config.provider, config.server_url) == (_SANDBOX["provider"], _SANDBOX["server_url"])


def test_no_sandbox_section_keeps_managed_hosts_off(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the section the boot is unchanged: managed hosts stay off."""
    exit_codes = request.getfixturevalue("boot_exit_codes")
    create_app = _boot_with_config(monkeypatch, tmp_path, None)

    assert exit_codes == []
    assert create_app.call_args.kwargs["sandbox_config"] is None


def test_invalid_sandbox_section_fails_startup(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed section stops the boot, matching ``omnigent server``.

    Failing here is the point: a typo must not surface as a 502 on the first
    managed session, and the app must never come up pretending the section
    was honored.
    """
    exit_codes = request.getfixturevalue("boot_exit_codes")
    with pytest.raises(SystemExit):
        _boot_with_config(monkeypatch, tmp_path, {"sandbox": "not-a-mapping"})

    assert exit_codes == [1]
    sys.modules.pop(_APP_MODULE, None)
