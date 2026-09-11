"""``omnigent run <agent>`` must not drop the spec's ``executor.model``.

When the effective config carries a harness default (``harness: pi``) and
``omnigent run <agent-dir>`` is invoked **without** ``--harness``, the CLI
fills the harness override from config and the override-materialization
path then pops ``executor.model`` from the rewritten bundle. The spec's
pinned model never reaches the provider wire: with a gateway provider that
declares only an ``openai`` family and no ``models.default``, turn setup
either fails loud with::

    No default model resolved for provider family 'openai': ... Set
    'executor.model' in the agent YAML or a provider 'models.default' ...

(although the agent YAML *does* set ``executor.model``), or — when catalog
discovery can resolve a family default — silently runs on that default
instead of the pinned model.

The test drives the real user journey end-to-end: seed an isolated config
home with the harness default and the gateway provider (pointed at the
mock LLM gateway), author an agent spec that pins ``executor.model``, run
the actual ``python -m omnigent run <agent-dir> -p 'say hi'`` subprocess
with no ``--harness`` flag, and require the journey to complete with the
spec's model reaching the gateway wire. Before the fix the spec model
never reaches the gateway ledger; after the fix the turn completes and the
captured chat-completions request carries the spec model.

No real LLM is needed — the provider's ``base_url`` targets the mock
gateway (``tests/server/integration/mock_llm_server.py``), which records
every request body::

    .venv/bin/python -m pytest \\
        tests/e2e/test_run_preserves_spec_model_under_config_harness_default.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import yaml

# Worktree root: tests/e2e/<this file> -> parents[2]. Threaded onto the CLI
# subprocess's PYTHONPATH so it (and the daemon/server/runner it spawns)
# import THIS worktree's code, not the editable install in a shared .venv.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# Unique, greppable model id so the gateway ledger assertion cannot match
# a request issued by anything but this spec.
_SPEC_MODEL = "e2e-spec-pinned-model"
_PROVIDER = "e2e-model-pin-gateway"
# Full local-stack budget: `omnigent run` boots a host daemon + local
# server + runner before the turn executes.
_RUN_TIMEOUT_S = 300


def _seed_config_home(config_home: Path, gateway_base_url: str) -> None:
    """Write the effective config that arms the bug.

    Two load-bearing properties:

    - ``harness: pi`` — a config-level harness *default*. ``omnigent run``
      fills the harness override from it when ``--harness`` is omitted,
      which is what the drop path mistakes for an explicit flag.
    - the provider declares only an ``openai`` family and **no**
      ``models.default`` — so once the spec model is dropped, nothing in
      the seeded config can resolve a model and the drop shows up either
      as a loud turn-setup failure or as a foreign catalog-default model
      on the gateway wire (never as the pinned model).

    :param config_home: Directory used as ``OMNIGENT_CONFIG_HOME``.
    :param gateway_base_url: OpenAI-wire base URL of the mock gateway,
        e.g. ``"http://127.0.0.1:51234/v1"``.
    """
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "harness": "pi",
                "providers": {
                    _PROVIDER: {
                        "kind": "gateway",
                        "openai": {
                            # A literal ref resolves to itself — no env
                            # propagation into the daemon/runner needed.
                            "api_key_ref": "dummy-key-model-pin",
                            "base_url": gateway_base_url,
                            "wire_api": "chat",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _seed_agent_dir(agent_dir: Path) -> None:
    """Write the agent bundle whose ``executor.model`` must survive.

    Mirrors the reported spec shape: ``executor.model`` pinned explicitly,
    provider auth pointing at the gateway, ``executor.config.harness``
    matching the config default (the drop fires on the override being
    *present*, not on it differing from the spec).

    :param agent_dir: Directory to create the bundle in.
    """
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "model-pin-agent",
                "description": "regression agent for the spec-model drop",
                "executor": {
                    "type": "omnigent",
                    "model": _SPEC_MODEL,
                    "auth": {"type": "provider", "name": _PROVIDER},
                    "config": {"harness": "pi"},
                },
                "prompt": "You are a terse test agent.\n",
                "os_env": {
                    "type": "caller_process",
                    "cwd": ".",
                    "sandbox": {"type": "none"},
                },
            }
        ),
        encoding="utf-8",
    )


def _gateway_request_models(mock_url: str) -> list[str]:
    """Model ids of every request the mock gateway captured.

    :param mock_url: Mock gateway base URL.
    :returns: ``model`` field of each captured request body, in order.
    """
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [r.get("model") for r in resp.json()["requests"] if isinstance(r, dict)]


def test_run_preserves_spec_model_under_config_harness_default(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """The spec's ``executor.model`` reaches the harness without ``--harness``.

    Journey (the reporter's, verbatim): config with a harness default and
    a gateway provider (openai family only, no ``models.default``) → agent
    spec pinning ``executor.model`` → ``omnigent run <agent-dir> -p ...``
    with no ``--harness`` flag. The run must not die at turn setup with
    ``No default model resolved`` (the field it asks for is set), and the
    request that reaches the gateway must carry the spec's model.
    """
    mock_url = isolated_mock_llm_server_url
    # Queue responses keyed by the spec model (the routing key is the
    # request's own ``model`` field, so a hit is itself evidence the spec
    # model survived), plus a fallback so an unexpected extra call cannot
    # starve the turn.
    httpx.post(
        f"{mock_url}/mock/configure",
        json={"key": _SPEC_MODEL, "responses": [{"text": "hi from mock"}] * 3},
        timeout=10.0,
    ).raise_for_status()
    httpx.post(
        f"{mock_url}/mock/set_fallback",
        json={"key": _SPEC_MODEL, "response": {"text": "hi from mock"}},
        timeout=10.0,
    ).raise_for_status()

    config_home = tmp_path / "confighome"
    _seed_config_home(config_home, f"{mock_url}/v1")
    agent_dir = tmp_path / "agent"
    _seed_agent_dir(agent_dir)
    home = tmp_path / "home"
    home.mkdir()

    # Nest the run's data dir under the session's throwaway
    # OMNIGENT_DATA_DIR (tests/conftest.py) so the session-end process
    # reaper can attribute any subprocess that survives a hard kill.
    session_data_root = Path(os.environ.get("OMNIGENT_DATA_DIR") or tmp_path)
    data_dir = session_data_root / f"model-pin-{uuid.uuid4().hex[:8]}"

    # The subprocess runs with cwd=tmp_path, where a shared venv's editable
    # install would win module resolution and silently test someone else's
    # checkout. Point it at this worktree instead (same pattern as
    # tests/e2e/_native_resume_helpers.cli_env). No trailing separator: an
    # empty PYTHONPATH element means cwd, re-introducing the shadowing.
    inherited_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = (
        f"{_REPO_ROOT}{os.pathsep}{inherited_pythonpath}"
        if inherited_pythonpath
        else str(_REPO_ROOT)
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(data_dir),
            "PYTHONPATH": pythonpath,
        }
    )
    # An ambient model override would re-inject a model after the drop and
    # mask the bug (see _apply_overrides_to_raw's OMNIGENT_MODEL fallback).
    env.pop("OMNIGENT_MODEL", None)

    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent",
                "run",
                str(agent_dir),
                "-p",
                "say hi",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=_RUN_TIMEOUT_S,
        )
    finally:
        # Tear down the host daemon + detached local server this run
        # booted for its isolated data dir.
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop"],
            capture_output=True,
            env=env,
            cwd=tmp_path,
            timeout=120,
        )

    combined = result.stdout + result.stderr

    # The bug's loud shape: turn setup demands the very field the agent
    # YAML already sets.
    assert "No default model resolved" not in combined, (
        "`omnigent run` (no --harness, config harness default set) dropped "
        f"the spec's executor.model={_SPEC_MODEL!r} at bundle write and "
        f"failed turn setup:\n{combined}"
    )
    assert result.returncode == 0, (
        "`omnigent run <agent> -p` exited non-zero although the agent spec "
        f"pins executor.model={_SPEC_MODEL!r} (exit {result.returncode}):\n"
        f"{combined}"
    )
    # The bug's silent shape: a catalog/provider default reaches the wire
    # instead of the pin. The spec model must be what actually reached the
    # gateway — highest precedence, ahead of provider/catalog defaults.
    models = _gateway_request_models(mock_url)
    assert _SPEC_MODEL in models, (
        "no request carrying the spec's executor.model reached the gateway "
        f"(captured request models: {models!r}):\n{combined}"
    )
