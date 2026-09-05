"""
``harness: genie`` wrap.

Thin module exposing :func:`create_app` — the entrypoint the shared
:mod:`omnigent.runtime.harnesses._runner` invokes after the parent process
resolves ``"genie"`` to this module via
:data:`omnigent.runtime.harnesses._HARNESS_MODULES`.

Mirrors :mod:`omnigent.inner.codex_harness` (genie is Databricks' fork of the
codex CLI); it instantiates an
:class:`omnigent.inner.genie_executor.GenieExecutor` (a thin
:class:`~omnigent.inner.codex_executor.CodexExecutor` subclass) from env vars
the parent process sets before spawning. Genie always routes through the
Databricks AI Gateway (Codex Responses API at ``/ai-gateway/codex/v1``), so the
gateway path defaults on; the base URL and ``databricks auth token`` auth
command are derived from the profile by the shared executor.

Env vars read at startup:

- ``HARNESS_GENIE_MODEL``: model identifier, e.g.
  ``"databricks-gpt-5-4-mini"``. ``None`` falls back to the catalog default.
- ``HARNESS_GENIE_GATEWAY``: ``"1"`` / ``"true"`` (default) to route through the
  Databricks AI gateway. ``"0"`` lets genie use its own ``~/.genie/config.toml``
  provider instead.
- ``HARNESS_GENIE_DATABRICKS_PROFILE``: ``~/.databrickscfg`` profile used to
  derive the gateway host / base URL and mint the bearer token.
- ``HARNESS_GENIE_GATEWAY_HOST`` / ``HARNESS_GENIE_GATEWAY_BASE_URL`` /
  ``HARNESS_GENIE_GATEWAY_AUTH_COMMAND`` /
  ``HARNESS_GENIE_GATEWAY_AUTH_REFRESH_INTERVAL_MS``: explicit gateway transport
  overrides (written by the Omnigent workflow layer); when unset the executor
  derives them from the Databricks profile.
- ``HARNESS_GENIE_CWD``: working directory the executor launches genie in.
  ``None`` falls back to ``OMNIGENT_RUNNER_WORKSPACE`` then the inherited cwd.
- ``OMNIGENT_GENIE_PATH``: absolute path to a ``genie`` CLI binary. ``None``
  searches ``PATH``.
- ``HARNESS_GENIE_ENABLE_WEB_SEARCH`` / ``HARNESS_GENIE_DISABLE_NATIVE_TOOLS`` /
  ``HARNESS_GENIE_OS_ENV`` / ``HARNESS_GENIE_RETRY_POLICY`` /
  ``HARNESS_GENIE_SKILLS_FILTER`` / ``HARNESS_GENIE_BUNDLE_DIR`` /
  ``HARNESS_GENIE_AGENT_NAME``: same semantics as their ``HARNESS_CODEX_*``
  counterparts (see :mod:`omnigent.inner.codex_harness`).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from fastapi import FastAPI

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.codex_harness import _parse_truthy
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import Executor
from omnigent.inner.genie_executor import GenieExecutor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.spec.types import RetryPolicy

_logger = logging.getLogger(__name__)

_AGENT_NAME = "genie"

_ENV_MODEL = "HARNESS_GENIE_MODEL"
_ENV_GATEWAY = "HARNESS_GENIE_GATEWAY"
_ENV_DATABRICKS_PROFILE = "HARNESS_GENIE_DATABRICKS_PROFILE"
_ENV_MODEL_PROVIDER = "HARNESS_GENIE_MODEL_PROVIDER"
_ENV_GATEWAY_HOST = "HARNESS_GENIE_GATEWAY_HOST"
_ENV_CWD = "HARNESS_GENIE_CWD"
_ENV_ENABLE_WEB_SEARCH = "HARNESS_GENIE_ENABLE_WEB_SEARCH"
_ENV_DISABLE_NATIVE_TOOLS = "HARNESS_GENIE_DISABLE_NATIVE_TOOLS"
_ENV_OS_ENV = "HARNESS_GENIE_OS_ENV"
_ENV_RETRY_POLICY = "HARNESS_GENIE_RETRY_POLICY"
_ENV_SKILLS_FILTER = "HARNESS_GENIE_SKILLS_FILTER"
_ENV_BUNDLE_DIR = "HARNESS_GENIE_BUNDLE_DIR"
_ENV_AGENT_NAME = "HARNESS_GENIE_AGENT_NAME"
_ENV_GATEWAY_BASE_URL = "HARNESS_GENIE_GATEWAY_BASE_URL"
_ENV_GATEWAY_AUTH_COMMAND = "HARNESS_GENIE_GATEWAY_AUTH_COMMAND"
_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS = "HARNESS_GENIE_GATEWAY_AUTH_REFRESH_INTERVAL_MS"


def _resolve_os_env() -> OSEnvSpec:
    """Resolve the inner-executor :class:`OSEnvSpec` from :data:`_ENV_OS_ENV`.

    Decodes the JSON-encoded :class:`OSEnvSpec` dict Omnigent serialized; on a
    missing/malformed value falls back to ``caller_process + sandbox=none`` so
    genie's natives stay enabled (matches the codex wrap's default).
    """
    raw = os.environ.get(_ENV_OS_ENV, "").strip()
    if raw:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            _logger.warning(
                "%s is not valid JSON (%s); falling back to default os_env", _ENV_OS_ENV, exc
            )
            payload = None
        if isinstance(payload, dict):
            sandbox_payload = payload.get("sandbox")
            sandbox = (
                OSEnvSandboxSpec(**sandbox_payload) if isinstance(sandbox_payload, dict) else None
            )
            return OSEnvSpec(
                type=str(payload.get("type", "caller_process")),
                cwd=payload.get("cwd"),
                sandbox=sandbox,
                fork=bool(payload.get("fork", False)),
            )
    return OSEnvSpec(
        type="caller_process", cwd=None, sandbox=OSEnvSandboxSpec(type="none"), fork=False
    )


def _resolve_retry_policy() -> RetryPolicy:
    """Resolve the :class:`RetryPolicy` from :data:`_ENV_RETRY_POLICY`.

    Falls back to ``RetryPolicy()`` when unset; a parse error degrades to the
    default with a warning rather than crashing.
    """
    raw = os.environ.get(_ENV_RETRY_POLICY, "").strip()
    if not raw:
        return RetryPolicy()
    try:
        return RetryPolicy.from_json(raw)
    except ValueError as exc:
        _logger.warning(
            "%s could not be parsed (%s); falling back to default RetryPolicy",
            _ENV_RETRY_POLICY,
            exc,
        )
        return RetryPolicy()


def _resolve_skills_filter() -> str | list[str]:
    """Resolve ``skills_filter`` from :data:`_ENV_SKILLS_FILTER` (default ``"all"``)."""
    raw = os.environ.get(_ENV_SKILLS_FILTER, "").strip()
    if not raw:
        return "all"
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "%s is not valid JSON (%s); falling back to 'all'", _ENV_SKILLS_FILTER, exc
        )
        return "all"
    if isinstance(decoded, str) and decoded in ("all", "none"):
        return decoded
    if isinstance(decoded, list) and all(isinstance(s, str) for s in decoded):
        return decoded
    _logger.warning(
        "%s decoded to unsupported shape %r; falling back to 'all'", _ENV_SKILLS_FILTER, decoded
    )
    return "all"


def _build_genie_executor() -> Executor:
    """Construct a :class:`GenieExecutor` from env-var config.

    Called lazily by the :class:`ExecutorAdapter` on the first turn.

    :raises ImportError: If the ``genie`` CLI isn't resolvable.
    :raises OSError: If the gateway is enabled but credentials are missing.
    """
    bundle_dir_raw = os.environ.get(_ENV_BUNDLE_DIR, "").strip()
    bundle_dir = Path(bundle_dir_raw) if bundle_dir_raw else None
    agent_name = os.environ.get(_ENV_AGENT_NAME, "").strip() or None
    return GenieExecutor(
        cwd=os.environ.get(_ENV_CWD) or os.environ.get("OMNIGENT_RUNNER_WORKSPACE") or None,
        os_env=_resolve_os_env(),
        model=os.environ.get(_ENV_MODEL),
        codex_path=resolve_harness_path("genie"),
        # Genie is the Databricks-gateway CLI; default the gateway path on.
        gateway=_parse_truthy(_ENV_GATEWAY, default=True),
        databricks_profile=os.environ.get(_ENV_DATABRICKS_PROFILE),
        model_provider_override=os.environ.get(_ENV_MODEL_PROVIDER) or None,
        gateway_host=os.environ.get(_ENV_GATEWAY_HOST) or None,
        enable_web_search=_parse_truthy(_ENV_ENABLE_WEB_SEARCH, default=True),
        disable_native_tools=_parse_truthy(_ENV_DISABLE_NATIVE_TOOLS, default=False),
        base_url_override=os.environ.get(_ENV_GATEWAY_BASE_URL) or None,
        gateway_auth_command=os.environ.get(_ENV_GATEWAY_AUTH_COMMAND) or None,
        gateway_auth_refresh_interval_ms=os.environ.get(_ENV_GATEWAY_AUTH_REFRESH_INTERVAL_MS)
        or None,
        retry_policy=_resolve_retry_policy(),
        bundle_dir=bundle_dir,
        agent_name=agent_name,
        skills_filter=_resolve_skills_filter(),
    )


def _materialize_genie_agent_spec(tmpdir: Path) -> Path:
    """
    Write the headless session agent spec used by Genie in the web UI picker.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to a generated YAML spec.
    """
    yaml_path = tmpdir / "genie.yaml"
    raw = {
        "name": _AGENT_NAME,
        "prompt": (
            "Genie is running as a headless coding agent through Omnigent. "
            "Web UI messages are forwarded to the Genie CLI."
        ),
        "executor": {
            "harness": "genie",
            # Conservative default; genie will negotiate the actual window
            # size via its API responses.
            "context_window": 200_000,
        },
    }
    # Write minimal YAML without external dependencies
    import yaml

    with open(yaml_path, "w") as f:
        yaml.dump(raw, f, default_flow_style=False)
    return yaml_path


def create_app() -> FastAPI:
    """Build the genie harness's FastAPI app (see the codex wrap's ``create_app``)."""
    adapter = ExecutorAdapter(executor_factory=_build_genie_executor)
    return adapter.build()
