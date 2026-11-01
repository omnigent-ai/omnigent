"""Explicit scheduler capabilities for automation browser journeys."""

from collections.abc import Iterator

import pytest

from tests._helpers.compat import compat_server_cwd, server_executable, server_pythonpath
from tests._helpers.live_server import isolated_local_server
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT


@pytest.fixture(scope="module")
def automations_v2_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Run a server with ``OMNIGENT_FEATURES=automations_v2`` and yield its URL.

    Tests that need an online host attach one explicitly. Run now only records
    a run, so a short poll keeps the wait for it to start brief.
    """
    server_dir = tmp_path_factory.mktemp("automations_v2_server")
    config_path = server_dir / "server.yaml"
    config_path.write_text("scheduled_task_scheduler:\n  poll_seconds: 1\n")
    with isolated_local_server(
        server_dir,
        env={
            "OMNIGENT_CONFIG": str(config_path),
            "OMNIGENT_FEATURES": "automations_v2",
            "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
            "PYTHONPATH": server_pythonpath(_REPO_ROOT),
        },
        executable=server_executable(),
        cwd=compat_server_cwd(),
        health_timeout=180.0,
    ) as base_url:
        yield base_url
