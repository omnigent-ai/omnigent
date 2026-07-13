"""Setup helpers for invoking ucode from Omnigent."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence

import click

from omnigent.onboarding.databricks_config import normalize_workspace_url
from omnigent.onboarding.ucode_state import read_ucode_state

_UCODE_AGENT_NAMES: tuple[str, ...] = ("claude", "codex", "pi")
# Pin to the ``main`` branch (not a SHA) so setup always tracks the latest
# ucode. A branch ref is mutable, so uvx would otherwise serve a stale cached
# build of an older ``main`` commit; ``--refresh-package ucode`` defeats that
# cache and forces re-resolution of the branch HEAD on every run.
_UCODE_GIT_REF = "main"
_UCODE_UVX_SOURCE = f"git+https://github.com/databricks/ucode@{_UCODE_GIT_REF}"


def model_gateway_workspace_urls() -> list[str]:
    """Return the workspaces ucode should configure for model serving.

    ucode's only job is wiring coding harnesses to the Unity AI gateway,
    so it only needs the gateway workspace(s) — the ``is_model_gateway``
    profiles. MCP-only workspaces (e.g. Jira / Confluence) still get a
    Databricks profile during onboarding for MCP auth, but passing them to
    ``ucode configure`` would make ucode do wasted gateway-discovery work
    (and extra per-workspace token fetches) against workspaces that serve
    no models.

    :returns: Gateway workspace URLs, each stripped of a trailing slash.
    """
    # Lazy import: internal-beta workspace list, excluded from the OSS build.
    from omnigent.onboarding.internal_beta import DEFAULT_PROFILES

    return [spec.host.rstrip("/") for spec in DEFAULT_PROFILES if spec.is_model_gateway]


def build_ucode_configure_command(
    ucode_command: Sequence[str],
    *,
    workspace_urls: Sequence[str],
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
) -> list[str]:
    """Build the ``ucode configure`` command Omnigent runs.

    :param ucode_command: Command prefix that invokes ucode, e.g.
        ``("/usr/bin/ucode",)`` or ``("uvx", "--refresh-package", "ucode",
        "--from", "git+https://github.com/databricks/ucode@main", "ucode")``.
    :param workspace_urls: Workspace URLs to configure. Must be non-empty.
    :param agents: ucode agent names to configure non-interactively,
        e.g. ``("claude", "codex", "pi")``.
    :returns: Command argv using ucode's comma-separated ``--workspaces``
        and ``--agents`` options.
    :raises ValueError: If *workspace_urls* is empty.
    """
    if not workspace_urls:
        raise ValueError("workspace_urls must not be empty")
    return [
        *ucode_command,
        "configure",
        "--workspaces",
        ",".join(normalize_workspace_url(url) for url in workspace_urls),
        "--agents",
        ",".join(agents),
    ]


def configure_ucode_for_workspace(
    workspace_url: str,
    *,
    agents: Sequence[str] = _UCODE_AGENT_NAMES,
) -> None:
    """Run ``ucode configure`` against a single model-serving workspace.

    This is the per-workspace counterpart to the legacy multi-workspace
    setup flow: instead of configuring every bundled profile at once, it
    wires the coding harnesses (Claude, Codex, Pi) to the Unity AI Gateway
    of exactly the one workspace the user supplied when adding a
    ``kind: databricks`` provider via ``omnigent setup --no-internal-beta``.
    ucode writes ``~/.ucode/state.json``, which Omnigent then reads for
    per-harness model defaults, base URLs, and the token-refresh command.

    :param workspace_url: The Databricks workspace URL whose model-serving
        gateway to configure, e.g.
        ``"https://example.databricks.com"``. A trailing slash
        is stripped by :func:`build_ucode_configure_command`.
    :param agents: ucode agent names to configure non-interactively,
        e.g. ``("claude", "codex", "pi")``. Defaults to all three.
    :returns: None.
    :raises click.ClickException: If ucode cannot be resolved (see
        :func:`find_ucode_command`) or ``ucode configure`` exits non-zero.
    """
    ucode_command = find_ucode_command()
    click.echo(f"Running `ucode configure --workspaces {workspace_url}`...")
    result = subprocess.run(
        build_ucode_configure_command(
            ucode_command, workspace_urls=[workspace_url], agents=agents
        ),
        check=False,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"`ucode configure` exited with code {result.returncode}; "
            "see the command output above for details."
        )
    click.echo("ucode configuration complete. Omnigent will use state.json for harness setup.")


def ucode_workspace_exists(workspace_url: str) -> bool:
    """Return whether ucode state contains *workspace_url*.

    :param workspace_url: Workspace URL to check, e.g.
        ``"https://example.databricks.com"``.
    :returns: ``True`` when ``~/.ucode/state.json`` has a readable
        entry for the workspace.
    """
    return read_ucode_state(workspace_url) is not None


def find_ucode_command() -> list[str]:
    """Return a command prefix that invokes ``ucode``.

    Prefers an ephemeral ``uvx`` run pinned to ucode's ``main`` branch so
    setup always uses the latest ucode rather than whatever (possibly stale)
    ``ucode`` the user installed long ago. A locally-installed binary is used
    only as a last resort when ``uvx`` is unavailable. This ordering matters:
    an old persistently-installed ``ucode`` predates options like
    ``configure --workspaces`` and would otherwise win and break setup.

    :returns: Command prefix, e.g.
        ``["uvx", "--refresh-package", "ucode", "--from",
        "git+https://github.com/databricks/ucode@main", "ucode"]`` or, when
        ``uvx`` is absent, ``["/usr/bin/ucode"]``.
    :raises click.ClickException: If neither ``uvx`` nor ``ucode`` is on PATH.
    """
    uvx = shutil.which("uvx")
    if uvx is not None:
        return [uvx, "--refresh-package", "ucode", "--from", _UCODE_UVX_SOURCE, "ucode"]

    ucode = shutil.which("ucode")
    if ucode is None:
        raise click.ClickException(
            "uvx is not on PATH and ucode is not installed. Install uv, then retry:\n"
            "  uv tool install uv  # provides uvx"
        )
    return [ucode]
