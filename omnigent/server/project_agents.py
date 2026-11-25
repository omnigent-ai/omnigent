"""Build and store a project role's agent: a copy of its base agent plus the role's settings.

A project agent is a ``kind = user`` agent owned by the project owner and
scoped to one project (``agents.project_id``). It is copied from its base when
it is built and doesn't follow later changes to the base. Bases are native
harness agents: the server's built-ins or the owner's own, unscoped agents.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import yaml

from omnigent.entities import Agent
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.agent_config import edit_agent_bundle
from omnigent.server.bundles import content_bundle_location, validate_agent_bundle
from omnigent.server.routes.session_mcp_servers import _tar_gz_dir
from omnigent.server.schemas import AgentConfigPatch
from omnigent.spec import AgentSpec, extract_safe
from omnigent.spec.parser import _ConfigYamlLoader
from omnigent.stores import AgentStore
from omnigent.stores.artifact_store import ArtifactStore

_CONFIG_FILE = "config.yaml"


def _refuse(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def check_base_agent(base: Agent, *, owner: str | None) -> None:
    """Refuse a base that would run another user's code, or a project agent.

    :raises OmnigentError: ``INVALID_INPUT`` for an ineligible base.
    """
    if base.project_id is not None:
        raise _refuse("A project agent can't be the base of another role")
    if base.operator_authored:
        return
    if base.kind != "user" or base.created_by != owner or base.session_id is not None:
        raise _refuse("A role can only be built on a server agent or one of your own agents")


def _standard_bundle(base_bundle: bytes, effort: str | None) -> bytes:
    """Rewrite *base_bundle*'s single spec file as ``config.yaml``, setting *effort*.

    Built-in native bundles carry ``<name>.yaml``; the agent editor needs
    ``config.yaml``. ``${VAR}`` is refused: only operator-authored agents expand
    it, so a copy would keep the literal text.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "agent"
        extract_safe(base_bundle, root)
        config_path = root / _CONFIG_FILE
        if not config_path.is_file():
            specs = [path for path in root.iterdir() if path.suffix in {".yaml", ".yml"}]
            if len(specs) != 1:
                raise _refuse("The base agent's bundle has no single spec file to copy")
            specs[0].rename(config_path)
        text = config_path.read_text(encoding="utf-8")
        if "${" in text:
            raise _refuse("The base agent uses ${...} settings, which a role's copy can't expand")
        config = yaml.load(text, Loader=_ConfigYamlLoader)
        original = dict(config)
        # Generated built-in specs use the shorthand form: no version, and the
        # harness directly under executor rather than in executor.config.
        config.setdefault("spec_version", 1)
        executor = dict(config.get("executor") or {})
        if "harness" in executor:
            executor["config"] = {
                "harness": executor.pop("harness"),
                **(executor.get("config") or {}),
            }
        if effort is not None:
            executor["reasoning_effort"] = effort
        config["executor"] = executor
        if config != original:
            config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        return _tar_gz_dir(root, original_bundle=base_bundle)


def build_role_agent_bundle(
    base_bundle: bytes,
    *,
    role_name: str,
    instructions: str,
    model: str | None = None,
    effort: str | None = None,
) -> tuple[bytes, AgentSpec]:
    """Copy a native base bundle and add a role's instructions, model and effort.

    The role's instructions follow the base's under a heading with the role's
    name. The copy keeps the base's spec name, which the agent editor treats as
    the agent's identity.

    :param base_bundle: The base agent's gzipped tarball.
    :param role_name: The seat's name, e.g. ``"Reviewer"``.
    :param instructions: The role's own instructions; empty adds none.
    :param model: Model for new sessions, or ``None`` to keep the base's.
    :param effort: Reasoning effort for new sessions, or ``None`` to keep the base's.
    :returns: The new bundle and its parsed spec.
    :raises OmnigentError: ``INVALID_INPUT`` for a non-native or ``${...}`` base.
    """
    base_spec = validate_agent_bundle(base_bundle, enforce_handler_allowlist=True)
    if native_coding_agent_for_harness(base_spec.executor.config.get("harness")) is None:
        raise _refuse("A role can only be built on a native coding agent")
    bundle = _standard_bundle(base_bundle, effort)
    patch: dict[str, str] = {}
    role_text = instructions.strip()
    if role_text:
        base_text = (base_spec.instructions or "").rstrip()
        section = f"## {role_name}\n\n{role_text}"
        patch["instructions"] = f"{base_text}\n\n{section}" if base_text else section
    if model is not None:
        patch["model"] = model
    return edit_agent_bundle(bundle, AgentConfigPatch(**patch))


def create_role_agent(
    agent_store: AgentStore,
    artifact_store: ArtifactStore,
    *,
    owner: str | None,
    project_id: str,
    base: Agent,
    role_name: str,
    instructions: str,
    model: str | None = None,
    effort: str | None = None,
) -> Agent:
    """Build a role's agent from *base* and store it as *owner*'s agent in *project_id*.

    :param owner: The project owner, who owns the agent; ``None`` on an auth-less server.
    :param base: The agent to copy, already checked with :func:`check_base_agent`.
    :returns: The stored project agent.
    """
    check_base_agent(base, owner=owner)
    base_bundle = artifact_store.get(base.bundle_location)
    bundle, spec = build_role_agent_bundle(
        base_bundle,
        role_name=role_name,
        instructions=instructions,
        model=model,
        effort=effort,
    )
    agent_id = uuid.uuid4().hex
    location = content_bundle_location(agent_id, bundle)
    artifact_store.put(location, bundle)
    return agent_store.create_project_agent(
        agent_id,
        role_name,
        location,
        owner=owner,
        project_id=project_id,
        description=spec.description,
    )
