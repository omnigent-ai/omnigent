"""Configuration helpers for UI test infrastructure, independent of browser fixtures."""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import textwrap
from collections.abc import Generator
from pathlib import Path

import pytest

from tests.helpers import ui_timings as timings
from tests.helpers.ui_url_safety import DEV_PORTS, unsafe_ui_base_url_reason

_ALLOW_DEV_BASE_URL_ENV = "OMNIGENT_E2E_ALLOW_DEV_BASE_URL"
_CONFIG_HOME_ENV = "OMNIGENT_CONFIG_HOME"
# Marks a config home created by this suite, so xdist workers replace it.
_SUITE_CONFIG_HOME_ENV = "OMNIGENT_UI_TESTS_CONFIG_HOME"
_CONFIG_HOME_KEY = pytest.StashKey[tuple[Path, dict[str, str | None]]]()
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"
_CODEX_MOCK_MODEL = "gpt-4o"
# USD per million tokens (input, output, cache read) for the mock codex provider.
_CODEX_MOCK_PRICING_PER_MILLION = (2.5, 10.0, 0.25)


class ServerState(dict[str, object]):
    def __missing__(self, key: str) -> object:
        if self.get("workflow_owned") and key in {
            "pid",
            "runner_pid",
            "database_uri",
            "restart_server",
            "binding_token",
        }:
            raise RuntimeError(
                f"Workflow-owned reproduction does not expose {key!r} to test fixtures. "
                "Use the product HTTP API, or run this process/database test in a "
                "separate fixture-owned environment outside dev.repro_env exec."
            )
        raise KeyError(key)


def prepared_repro_environment() -> dict[str, str]:
    keys = ("OMNIGENT_REPRO_SERVER_URL", "OMNIGENT_REPRO_MODEL_URL", "OMNIGENT_REPRO_RUNNER_ID")
    values = {key: os.environ.get(key, "") for key in keys}
    if any(values.values()) and not all(values.values()):
        missing = ", ".join(key for key, value in values.items() if not value)
        raise RuntimeError(
            f"Incomplete prepared reproduction environment: missing {missing}. "
            "Launch tests via python -m dev.repro_env exec -- ..."
        )
    return values


def pytest_configure(config: pytest.Config) -> None:
    """Fail fast on unsafe e2e-ui harness options and isolate per-process state.

    :param config: Pytest config with repo and pytest-playwright options.
    """
    timings.pytest_configure(config)
    base_url = config.getoption("--ui-base-url")
    if base_url:
        _validate_ui_base_url(base_url)

    if os.environ.get("CI") and config.getoption("--headed", default=False):
        raise pytest.UsageError(
            "tests/e2e_ui must run headless in CI. Remove --headed; headed "
            "browser windows are only allowed for local debugging."
        )
    _isolate_config_home(config)
    _isolate_playwright_output(config)


# Run after the root conftest reaps leaked processes that may still use this home.
@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore the inherited config home and remove this process's private copy."""
    if (owned := config.stash.get(_CONFIG_HOME_KEY, None)) is None:
        return
    home, originals = owned
    for name, value in originals.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    shutil.rmtree(home, ignore_errors=True)


def _isolate_config_home(config: pytest.Config) -> None:
    """Give this pytest process a private config home seeded from the effective config.

    Spawned servers, runners and CLIs inherit it, so concurrent xdist workers
    cannot overwrite each other's mock provider config. A caller-chosen home,
    an external server and a prepared reproduction keep their own config.
    """
    # The product treats an empty value as unset, so this does too.
    inherited = os.environ.get(_CONFIG_HOME_ENV) or None
    suite_owned = inherited is not None and inherited == os.environ.get(_SUITE_CONFIG_HOME_ENV)
    if (inherited is not None and not suite_owned) or (
        config.getoption("--ui-base-url", default=None)
        or os.environ.get("OMNIGENT_REPRO_SERVER_URL")
        # An xdist controller spawns nothing; each worker makes its own copy.
        or (config.getoption("numprocesses", default=0) and timings.xdist_worker(config) is None)
    ):
        return
    from omnigent.config import global_config_path

    source = global_config_path().parent
    home = Path(tempfile.mkdtemp(prefix="omnigent-ui-config-"))
    # File-backed provider secrets live beside the config they resolve for.
    for name in ("config.yaml", "secrets.json"):
        if (source / name).is_file():
            fd = os.open(home / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write((source / name).read_bytes())
    originals = {name: os.environ.get(name) for name in (_CONFIG_HOME_ENV, _SUITE_CONFIG_HOME_ENV)}
    os.environ[_CONFIG_HOME_ENV] = os.environ[_SUITE_CONFIG_HOME_ENV] = str(home)
    config.stash[_CONFIG_HOME_KEY] = (home, originals)


def _isolate_playwright_output(config: pytest.Config) -> None:
    """Keep each xdist worker's Playwright artifacts in its own directory.

    pytest-playwright clears ``--output`` when every worker's session starts, so
    a shared directory would let one worker delete another's failure traces.
    """
    output = config.getoption("--output", default=None)
    if output is None:
        return
    if worker := timings.xdist_worker(config):
        config.option.output = os.path.join(output, worker)
    elif config.getoption("numprocesses", default=0):
        shutil.rmtree(output, ignore_errors=True)


def _validate_ui_base_url(base_url: str) -> None:
    reason = unsafe_ui_base_url_reason(base_url)
    if reason is None or os.environ.get(_ALLOW_DEV_BASE_URL_ENV) == "1":
        return
    dev_ports = ", ".join(str(port) for port in sorted(DEV_PORTS))
    raise pytest.UsageError(
        f"Refusing --ui-base-url={base_url!r}: {reason}. Reusing a dev or "
        "production-like server is unsafe because e2e UI tests share that "
        "server's database, artifacts, and runner state. Omit --ui-base-url "
        "to let the fixture spawn an isolated server on a random port. If "
        "you intentionally want to reuse this server for local debugging, "
        f"set {_ALLOW_DEV_BASE_URL_ENV}=1. Refused dev ports: {dev_ports}."
    )


def validate_native_claude_mock_routing(mock_llm_server_url: str) -> None:
    """Reject a known machine policy redirect before launching the real Claude CLI.

    Callers must opt in to the ``claude_managed_settings`` fixture so pytest's
    ambient isolation does not hide settings the child process will still read.
    """
    from omnigent.onboarding.ambient import claude_managed_gateway

    managed_url, _ = claude_managed_gateway()
    if managed_url and managed_url.rstrip("/") != mock_llm_server_url.rstrip("/"):
        raise RuntimeError(
            "Claude Code's managed ANTHROPIC_BASE_URL overrides the mock provider. "
            "CLAUDE_CONFIG_DIR does not isolate machine-managed settings. "
            "Run this mock journey in a clean container or CI runner without that override; "
            "do not change machine policy. No Claude session was launched."
        )


@contextlib.contextmanager
def temp_omnigent_mock_config(
    mock_llm_server_url: str, harness: str, *, workflow_owned: bool = False
) -> Generator[None, None, None]:
    """Temporarily write a mock provider config in the selected config home.

    Native credential helpers may read provider configuration on every turn,
    so the mock config stays in place for the fixture's full lifetime.
    Restores the original file (or removes it) on exit.

    :param mock_llm_server_url: Base URL of the mock LLM server, e.g.
        ``"http://127.0.0.1:51235"``.
    :param harness: ``"claude"`` or ``"codex"``.
    :param workflow_owned: Reuse the provider configuration prepared by the workflow.
    """
    if workflow_owned:
        yield
        return

    from omnigent.config import global_config_path

    # Back up and restore the target without replacing a user's config symlink.
    config_path = global_config_path().resolve()
    config_dir = config_path.parent
    config_dir.mkdir(parents=True, exist_ok=True)
    backup = config_path.with_name(config_path.name + ".e2e-backup")
    if backup.exists():
        raise RuntimeError(
            f"Unrestored mock-provider backup at {backup}; "
            "recover the original config before retrying"
        )
    original = config_path.read_bytes() if config_path.exists() else None

    if harness == "claude":
        mock_config = textwrap.dedent(f"""\
            providers:
              mock-claude:
                kind: key
                default: [anthropic]
                anthropic:
                  base_url: "{mock_llm_server_url}"
                  api_key: "mock-key"
                  models:
                    default: {_CLAUDE_MOCK_MODEL}
            """)
    else:  # codex
        # The mock model is not in the pricing catalog; configured rates let
        # the server price codex-native sessions (Session cost, per-model cost).
        mock_config = textwrap.dedent(f"""\
            providers:
              mock-codex:
                kind: key
                default: [openai]
                openai:
                  base_url: "{mock_llm_server_url}/v1"
                  api_key: "mock-key"
                  wire_api: responses
                  models:
                    default: {_CODEX_MOCK_MODEL}
                  pricing:
                    input_per_million: {_CODEX_MOCK_PRICING_PER_MILLION[0]}
                    output_per_million: {_CODEX_MOCK_PRICING_PER_MILLION[1]}
                    cache_read_per_million: {_CODEX_MOCK_PRICING_PER_MILLION[2]}
            """)

    if original is not None:
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
    try:
        config_path.write_text(mock_config)
        yield
    finally:
        if original is not None:
            backup.replace(config_path)
        else:
            config_path.unlink(missing_ok=True)
