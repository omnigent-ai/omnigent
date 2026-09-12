"""Host-side provider and agent-pin operations for the config control plane.

Implements the operations behind the ``host.provider_op`` frame: provider
CRUD and endpoint probes against this machine's ``~/.omnigent/config.yaml``,
plus per-agent provider/model pins against the agent specs under
``~/.omnigent/agents/``. The server's ``/v1/hosts/{id}/providers*`` and
``/v1/hosts/{id}/agents/{name}/pin`` routes drive these remotely (issue
omnigent-ai/omnigent#7134); nothing here trusts the wire for validation —
every mutation re-runs the same parser the CLI and runtime use
(:func:`omnigent.onboarding.provider_config._parse_provider`), so a config
file this module writes is one the runtime accepts.

Secret handling: entries may legitimately carry an inline ``api_key``
(the same thing ``omnigent setup`` can write), but no operation here ever
returns secret *values* — listings and mutation results redact them to
source descriptors (``api_key_set`` / ``api_key_ref`` / ``auth_command``).
Endpoint probes resolve the credential on the host and send it only to
the provider's own ``base_url``.

Every mutating write backs the target file up first
(``<name>.bak-<epoch>``) so a bad panel edit is one file-copy away from
recovery.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

import httpx
import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.provider_config import (
    _config_path,
    _parse_provider,
    _VALID_FAMILIES,
    resolve_secret,
)

_logger = logging.getLogger(__name__)

# ``omnigent run`` resolves single-file and bundle agent specs from here
# (cli.py ``_GLOBAL_AGENTS_DIR``). Resolved at call time so tests can point
# the ops at a temporary directory.
_TEST_HTTP_TIMEOUT_S = 10.0
_TEST_MODELS_SAMPLE = 50


def _agents_dir() -> Path:
    """Return this host's agent-spec directory.

    Respects ``$OMNIGENT_CONFIG_HOME`` for test isolation (matching
    :func:`omnigent.onboarding.provider_config._config_path`), so the
    frame handler and the functions it calls stay consistent about where
    this host's ``~/.omnigent`` actually is.

    :returns: ``~/.omnigent/agents`` (or ``$OMNIGENT_CONFIG_HOME/agents``).
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "agents"
    return Path.home() / ".omnigent" / "agents"


def _load_config_mapping(config_path: str | None = None) -> dict[str, Any]:
    """Load ``config.yaml`` as a mapping, creating nothing.

    :param config_path: Explicit config path (tests); ``None`` uses
        :func:`omnigent.onboarding.provider_config._config_path`.
    :returns: The parsed mapping; ``{}`` when the file does not exist yet.
    :raises OmnigentError: When the file exists but is not a YAML mapping.
    """
    path = config_path or _config_path()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = yaml.safe_load(f)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"{path}: expected a YAML mapping at top level, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _backup(path: str) -> str | None:
    """Copy *path* to a timestamped ``.bak-<epoch>`` sibling.

    :param path: File about to be overwritten.
    :returns: The backup path, or ``None`` when there was nothing to back up.
    """
    if not os.path.exists(path):
        return None
    backup = f"{path}.bak-{int(time.time())}"
    shutil.copy2(path, backup)
    return backup


def _write_config(mapping: Mapping[str, Any], config_path: str | None = None) -> str:
    """Atomically-ish write *mapping* back to ``config.yaml``.

    Key order of the loaded mapping is preserved (``sort_keys=False``) so
    round-trips do not reshuffle a user's hand-edited file.

    :param mapping: The new top-level config mapping.
    :param config_path: Explicit config path (tests); ``None`` uses the
        global config path.
    :returns: The backup path written before the new content.
    """
    path = config_path or _config_path()
    backup = _backup(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        yaml.safe_dump(dict(mapping), f, sort_keys=False)
    os.replace(tmp, path)
    return backup or ""


def _redact_entry(name: str, raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a provider entry safe to show to the panel.

    Secret *values* (an inline ``api_key``, resolved ``auth_command``
    output) never cross the wire — including inside family blocks, where
    the credential actually lives. Each secret source is described
    (``api_key_set``), not revealed; ``api_key_ref`` / ``auth_command``
    are references, not secrets, and pass through verbatim.

    :param name: The provider's key under ``providers:``.
    :param raw: The raw entry mapping from the config file.
    :returns: Redacted copy with ``name`` added.
    """
    out: dict[str, Any] = {"name": name}
    for key, value in raw.items():
        if key == "api_key":
            out["api_key_set"] = True
            continue
        if key in _VALID_FAMILIES and isinstance(value, Mapping):
            family_out: dict[str, Any] = {}
            for fkey, fvalue in value.items():
                if fkey == "api_key":
                    family_out["api_key_set"] = True
                else:
                    family_out[fkey] = fvalue
            out[key] = family_out
            continue
        out[key] = value
    return out


def _families_of(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return the inline family blocks of a raw provider entry.

    :param raw: The raw entry mapping.
    :returns: Family-name → family-block mappings present on the entry.
    """
    return {k: v for k, v in raw.items() if k in _VALID_FAMILIES and isinstance(v, dict)}


# ── providers ops ────────────────────────────────────────


def providers_list(config_path: str | None = None) -> dict[str, Any]:
    """List this host's providers, secrets redacted.

    :param config_path: Explicit config path (tests).
    :returns: ``{"providers": [...], "config_path": str}``.
    """
    config = _load_config_mapping(config_path)
    raw_providers = config.get("providers")
    entries: list[dict[str, Any]] = []
    if isinstance(raw_providers, dict):
        for name, raw in raw_providers.items():
            if isinstance(raw, dict):
                entries.append(_redact_entry(str(name), raw))
    return {"providers": entries, "config_path": config_path or _config_path()}


def provider_upsert(name: str, entry: Mapping[str, Any], config_path: str | None = None) -> dict[str, Any]:
    """Insert or replace one provider entry after full validation.

    The whole entry is replaced (no field-level merge): the panel edits a
    complete form, and replace-over-merge keeps the file's semantics equal
    to a hand edit. Validation reuses :func:`_parse_provider` — the same
    parser every turn runs — so an entry this function writes is one the
    runtime accepts.

    :param name: The provider key under ``providers:``. Must be a
        non-empty string without path separators.
    :param entry: The new raw entry mapping (``kind``, families, ...).
    :param config_path: Explicit config path (tests).
    :returns: ``{"name": ..., "provider": <redacted>, "backup": <path or "">}``.
    :raises OmnigentError: ``INVALID_INPUT`` for a bad name or an entry
        the shared parser rejects.
    """
    if not isinstance(name, str) or not name.strip() or "/" in name or name.startswith("."):
        raise OmnigentError(f"invalid provider name {name!r}", code=ErrorCode.INVALID_INPUT)
    name = name.strip()
    if not isinstance(entry, dict):
        raise OmnigentError(
            f"provider {name!r}: entry must be a mapping", code=ErrorCode.INVALID_INPUT
        )
    # Validate through the runtime's own parser before touching the file.
    _parse_provider(name, dict(entry))
    config = _load_config_mapping(config_path)
    providers = config.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        config["providers"] = providers
    providers[name] = dict(entry)
    backup = _write_config(config, config_path)
    _logger.info("provider %r upserted via control plane (backup %s)", name, backup or "n/a")
    return {"name": name, "provider": _redact_entry(name, entry), "backup": backup}


def provider_delete(name: str, config_path: str | None = None) -> dict[str, Any]:
    """Delete one provider entry.

    :param name: The provider key under ``providers:``.
    :param config_path: Explicit config path (tests).
    :returns: ``{"name": ..., "backup": <path or "">}``.
    :raises OmnigentError: ``NOT_FOUND`` when no such provider exists.
    """
    config = _load_config_mapping(config_path)
    providers = config.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        raise OmnigentError(f"no provider named {name!r}", code=ErrorCode.NOT_FOUND)
    removed = providers.pop(name)
    backup = _write_config(config, config_path)
    _logger.info("provider %r deleted via control plane (backup %s)", name, backup or "n/a")
    return {"name": name, "removed": _redact_entry(name, removed) if isinstance(removed, dict) else None, "backup": backup}


def provider_test(name: str, config_path: str | None = None) -> dict[str, Any]:
    """Probe a provider's endpoint with its own credential.

    Resolves the credential exactly as a turn would (:func:`resolve_secret`
    for ``api_key_ref``, ``$VAR`` expansion for inline keys, subcommand
    execution for ``auth_command``) and issues one authenticated
    ``GET {base_url}/models``. The credential value is sent only to the
    provider's own ``base_url`` and is never included in the result.

    :param name: The provider key under ``providers:``.
    :param config_path: Explicit config path (tests).
    :returns: ``{"name", "family", "endpoint", "http_status", "ok",
        "latency_ms", "models"}`` — ``models`` is a bounded sample of the
        ids the endpoint advertises.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown provider;
        ``INVALID_INPUT`` for kinds without an endpoint (subscription,
        databricks, cli-config, bedrock) or a family that cannot be
        probed.
    """
    config = _load_config_mapping(config_path)
    providers = config.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        raise OmnigentError(f"no provider named {name!r}", code=ErrorCode.NOT_FOUND)
    raw = providers[name]
    if not isinstance(raw, dict):
        raise OmnigentError(f"provider {name!r} is not a mapping", code=ErrorCode.INVALID_INPUT)
    kind = raw.get("kind")
    if kind in ("subscription", "databricks", "cli-config", "bedrock"):
        raise OmnigentError(
            f"provider {name!r} has kind {kind!r} with no endpoint to probe",
            code=ErrorCode.INVALID_INPUT,
        )
    families = _families_of(raw)
    if not families:
        raise OmnigentError(
            f"provider {name!r} declares no {'/'.join(_VALID_FAMILIES)} family to probe",
            code=ErrorCode.INVALID_INPUT,
        )
    # Prefer the family whose wire format is our probe format; else the
    # first declared family (deterministic given the entry's key order).
    family = next((f for f in families if f == "openai"), next(iter(families)))
    block = families[family]
    base_url = block.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        raise OmnigentError(
            f"provider {name!r} family {family!r} has no base_url",
            code=ErrorCode.INVALID_INPUT,
        )

    secret: str | None = None
    if isinstance(block.get("api_key_ref"), str):
        secret = resolve_secret(block["api_key_ref"])
    elif isinstance(block.get("api_key"), str):
        secret = resolve_secret(block["api_key"])
    elif isinstance(block.get("auth_command"), str):
        proc = subprocess.run(
            block["auth_command"], shell=True, capture_output=True, text=True, timeout=15
        )
        if proc.returncode != 0:
            raise OmnigentError(
                f"auth_command for {name!r} failed: {proc.stderr.strip()[:200]}",
                code=ErrorCode.INVALID_INPUT,
            )
        secret = proc.stdout.strip()

    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    started = time.monotonic()
    try:
        response = httpx.get(url, headers=headers, timeout=_TEST_HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return {
            "name": name,
            "family": family,
            "endpoint": url,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    latency_ms = int((time.monotonic() - started) * 1000)
    models: list[str] = []
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            body = None
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, list):
            models = [
                str(item.get("id"))
                for item in data[:_TEST_MODELS_SAMPLE]
                if isinstance(item, dict) and item.get("id")
            ]
    return {
        "name": name,
        "family": family,
        "endpoint": url,
        "http_status": response.status_code,
        "ok": response.status_code == 200,
        "latency_ms": latency_ms,
        "models": models,
    }


# ── agent pin ops ────────────────────────────────────────


def _agent_specs(agents_dir: Path) -> list[tuple[str, Path]]:
    """Enumerate agent spec files under *agents_dir*.

    :param agents_dir: The agent directory to scan.
    :returns: ``(agent_name, spec_path)`` pairs — bundle directories
        (``<dir>/config.yaml``) and single-file ``*.yaml`` specs.
    """
    if not agents_dir.is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(agents_dir.iterdir()):
        if child.is_dir():
            config = child / "config.yaml"
            if config.is_file():
                found.append((child.name, config))
        elif child.suffix in (".yaml", ".yml"):
            found.append((child.stem, child))
    return found


def _load_agent_spec(path: Path) -> dict[str, Any]:
    """Load one agent spec file as a mapping.

    :param path: The spec file (bundle ``config.yaml`` or single-file YAML).
    :returns: The parsed mapping.
    :raises OmnigentError: ``NOT_FOUND`` when missing, ``INVALID_INPUT``
        when the YAML is not a mapping.
    """
    if not path.is_file():
        raise OmnigentError(f"agent spec not found: {path}", code=ErrorCode.NOT_FOUND)
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise OmnigentError(
            f"{path}: expected a YAML mapping at top level, got {type(raw).__name__}",
            code=ErrorCode.INVALID_INPUT,
        )
    return raw


def _spec_summary(name: str, raw: Mapping[str, Any], path: Path) -> dict[str, Any]:
    """Summarize one agent spec for listings.

    :param name: The agent's directory / file-stem name.
    :param raw: The parsed spec mapping.
    :param path: Where the spec lives (for the response's ``path`` hint).
    :returns: ``{"name", "harness", "model", "auth", "spec_version", "path"}``.
    """
    executor = raw.get("executor")
    executor = executor if isinstance(executor, dict) else {}
    config = executor.get("config")
    config = config if isinstance(config, dict) else {}
    harness = executor.get("harness") if "spec_version" not in raw else config.get("harness")
    auth = executor.get("auth")
    auth_out: Any = None
    if isinstance(auth, dict):
        auth_out = {"type": auth.get("type")}
        if auth.get("type") == "provider":
            auth_out["name"] = auth.get("name")
    return {
        "name": name,
        "harness": harness if isinstance(harness, str) else None,
        "model": executor.get("model") if isinstance(executor.get("model"), str) else None,
        "auth": auth_out,
        "spec_version": raw.get("spec_version"),
        "path": str(path),
    }


def agents_list(agents_dir: str | None = None) -> dict[str, Any]:
    """List the agent specs on this host with their current pins.

    :param agents_dir: Explicit agents directory (tests); ``None`` uses
        ``~/.omnigent/agents``.
    :returns: ``{"agents": [...], "agents_dir": str}``.
    """
    root = Path(agents_dir) if agents_dir else _agents_dir()
    agents = []
    for name, path in _agent_specs(root):
        try:
            raw = _load_agent_spec(path)
        except OmnigentError:
            continue
        agents.append(_spec_summary(name, raw, path))
    return {"agents": agents, "agents_dir": str(root)}


def agent_pin_set(
    agent: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    agents_dir: str | None = None,
) -> dict[str, Any]:
    """Pin one agent spec's provider and/or model.

    Writes the pin where the spec format reads it: ``executor.auth =
    {type: provider, name: <provider>}`` — the strongest per-spec
    provider selector the runtime knows (fail-loud when the provider is
    undeclared) — and ``executor.model`` for the model id. The agent's
    other fields are untouched; the file is backed up first.

    :param agent: The agent's directory / file-stem name under the
        agents dir.
    :param provider: Provider name to pin, or ``None`` to leave the
        provider selection unchanged.
    :param model: Model id to pin, or ``None`` to leave the model
        unchanged.
    :param agents_dir: Explicit agents directory (tests).
    :returns: ``{"agent": ..., "spec": <summary after the write>,
        "backup": <path or "">}``.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown agent;
        ``INVALID_INPUT`` for an empty pin request or a bad name.
    """
    if not provider and not model:
        raise OmnigentError(
            "agent_pin_set requires a provider and/or model", code=ErrorCode.INVALID_INPUT
        )
    root = Path(agents_dir) if agents_dir else _agents_dir()
    matches = [(n, p) for n, p in _agent_specs(root) if n == agent]
    if not matches:
        raise OmnigentError(f"no agent named {agent!r} on this host", code=ErrorCode.NOT_FOUND)
    if provider is not None and (
        not isinstance(provider, str)
        or not provider.strip()
        or "/" in provider
        or provider.startswith(".")
    ):
        raise OmnigentError(f"invalid provider name {provider!r}", code=ErrorCode.INVALID_INPUT)
    _, path = matches[0]
    raw = _load_agent_spec(path)
    executor = raw.get("executor")
    if not isinstance(executor, dict):
        executor = {}
        raw["executor"] = executor
    if provider is not None:
        executor["auth"] = {"type": "provider", "name": provider.strip()}
    if model is not None:
        if not isinstance(model, str) or not model.strip():
            raise OmnigentError(f"invalid model id {model!r}", code=ErrorCode.INVALID_INPUT)
        executor["model"] = model.strip()
    backup = _backup(str(path))
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    os.replace(tmp, path)
    _logger.info(
        "agent %r pinned (provider=%r model=%r) via control plane", agent, provider, model
    )
    return {
        "agent": agent,
        "spec": _spec_summary(agent, raw, path),
        "backup": backup or "",
    }


def agent_pin_clear(
    agent: str,
    *,
    provider: bool = True,
    model: bool = True,
    agents_dir: str | None = None,
) -> dict[str, Any]:
    """Remove one agent spec's provider and/or model pin.

    Only a ``provider``-typed ``executor.auth`` is removed — an inline
    ``api_key`` auth block is left alone (the panel never silently drops
    a credential the user pasted into the spec).

    :param agent: The agent's directory / file-stem name.
    :param provider: Also remove the provider pin.
    :param model: Also remove the model pin.
    :param agents_dir: Explicit agents directory (tests).
    :returns: ``{"agent": ..., "spec": <summary after the write>,
        "backup": <path or "">}``.
    :raises OmnigentError: ``NOT_FOUND`` for an unknown agent;
        ``INVALID_INPUT`` when nothing was requested.
    """
    if not provider and not model:
        raise OmnigentError(
            "agent_pin_clear requires provider and/or model", code=ErrorCode.INVALID_INPUT
        )
    root = Path(agents_dir) if agents_dir else _agents_dir()
    matches = [(n, p) for n, p in _agent_specs(root) if n == agent]
    if not matches:
        raise OmnigentError(f"no agent named {agent!r} on this host", code=ErrorCode.NOT_FOUND)
    _, path = matches[0]
    raw = _load_agent_spec(path)
    executor = raw.get("executor")
    if isinstance(executor, dict):
        if provider:
            auth = executor.get("auth")
            if isinstance(auth, dict) and auth.get("type") == "provider":
                del executor["auth"]
        if model and "model" in executor:
            del executor["model"]
    backup = _backup(str(path))
    tmp = f"{path}.tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    os.replace(tmp, path)
    _logger.info("agent %r pins cleared (provider=%s model=%s)", agent, provider, model)
    return {
        "agent": agent,
        "spec": _spec_summary(agent, raw, path),
        "backup": backup or "",
    }


# ── op dispatch ──────────────────────────────────────────

# The wire-dispatch table deliberately does NOT honor ``config_path`` /
# ``agents_dir`` keys from ``frame.params``: an operation must always run
# against this host's real ``~/.omnigent`` files, never an arbitrary path a
# remote caller names. The explicit-path parameters on the functions above
# exist for tests (which call the functions directly) and for future
# in-process callers under the same trust boundary.
_PROVIDER_OPS = {
    "providers_list": lambda params: providers_list(),
    "provider_upsert": lambda params: provider_upsert(
        params.get("name"),
        params.get("entry") if isinstance(params.get("entry"), dict) else {},
    ),
    "provider_delete": lambda params: provider_delete(params.get("name")),
    "provider_test": lambda params: provider_test(params.get("name")),
    "agents_list": lambda params: agents_list(),
    "agent_pin_set": lambda params: agent_pin_set(
        params.get("agent"),
        provider=params.get("provider") if isinstance(params.get("provider"), str) else None,
        model=params.get("model") if isinstance(params.get("model"), str) else None,
    ),
    "agent_pin_clear": lambda params: agent_pin_clear(
        params.get("agent"),
        provider=bool(params.get("provider", True)),
        model=bool(params.get("model", True)),
    ),
}


def run_provider_op(op: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """Run one named provider/agent-pin operation.

    The single entry point the ``host.provider_op`` frame handler calls.

    :param op: One of the op names in ``_PROVIDER_OPS``.
    :param params: Op-specific arguments.
    :returns: The op's result payload.
    :raises OmnigentError: ``INVALID_INPUT`` for an unknown op; the
        underlying op's errors otherwise.
    """
    handler = _PROVIDER_OPS.get(op)
    if handler is None:
        raise OmnigentError(f"unknown provider op {op!r}", code=ErrorCode.INVALID_INPUT)
    return handler(dict(params))
