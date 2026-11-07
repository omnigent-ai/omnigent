"""Attach policies from a YAML file to a harness session (``--policies``).

Harness commands such as ``omnigent pi`` start their own session, so the
server-wide ``policies`` config never reaches them. ``--policies FILE`` lets a
user scope policies to the one session the command starts or resumes. The file
uses the same fields as ``POST /v1/sessions/{session_id}/policies``::

    policies:
      - name: cap-tool-calls
        type: python
        handler: omnigent.policies.builtins.safety.max_tool_calls_per_session
        factory_params: {limit: 20}

The file is validated when the command starts, before any server work, so a
typo fails fast instead of after the runner is up. The server still applies its
own checks (handler allowlist, ``factory_params`` schema) when each policy is
attached.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

import click

from omnigent.util.json_types import JsonObject

if TYPE_CHECKING:
    import httpx
    from pydantic import ValidationError

# Only ``policies_option`` runs at CLI import time; YAML, pydantic, httpx and
# the server schemas are imported inside the functions that need them.

_F = TypeVar("_F", bound=Callable[..., Any])  # type: ignore[explicit-any]


def policies_option(func: _F) -> _F:
    """Add the shared ``--policies FILE`` option to a harness command.

    :param func: The Click command callback; it receives ``policies_file``.
    :returns: The callback with the option attached.
    """
    return click.option(
        "--policies",
        "policies_file",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        default=None,
        help=(
            "YAML file of policies to attach to this session, e.g. "
            "``policies: [{name, type, handler, factory_params}]``. Same fields "
            "as POST /v1/sessions/{id}/policies."
        ),
    )(func)


def load_policies_file(path: Path) -> list[JsonObject]:
    """Read and validate a ``--policies`` file.

    :param path: YAML file with a top-level ``policies`` list.
    :returns: One request body per policy, ready to POST.
    :raises click.ClickException: If the file is not valid YAML, has no
        ``policies`` list, repeats a name, or an entry fails validation.
    """
    import yaml
    from pydantic import ValidationError

    from omnigent.server.schemas import CreateSessionPolicyRequest

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise click.ClickException(f"--policies {path}: not valid YAML: {exc}") from exc
    entries = raw.get("policies") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise click.ClickException(
            f"--policies {path}: expected a top-level 'policies:' list of entries "
            "with name, type, handler and optional factory_params."
        )
    bodies: list[JsonObject] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise click.ClickException(f"--policies {path}: entry {index} is not a mapping.")
        try:
            request = CreateSessionPolicyRequest.model_validate(entry)
        except ValidationError as exc:
            raise click.ClickException(
                f"--policies {path}: entry {index} is invalid: {_first_error(exc)}"
            ) from exc
        if request.name in seen:
            raise click.ClickException(
                f"--policies {path}: policy name {request.name!r} appears twice; "
                "names must be unique within a session."
            )
        seen.add(request.name)
        bodies.append(request.model_dump(exclude_none=True))
    return bodies


def _first_error(exc: ValidationError) -> str:
    """Render the first pydantic error as ``field: message``.

    :param exc: The validation error.
    :returns: A one-line description, e.g. ``"handler: Field required"``.
    """
    error = exc.errors()[0]
    location = ".".join(str(part) for part in error.get("loc", ())) or "entry"
    return f"{location}: {error.get('msg', 'invalid')}"


async def apply_session_policies(
    client: httpx.AsyncClient,
    session_id: str,
    policies: list[JsonObject],
) -> None:
    """Attach each policy to a session.

    A policy whose name is already attached (409, e.g. when resuming a
    session started with the same file) is left as it is and reported.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: The session to attach to, e.g. ``"conv_abc123"``.
    :param policies: Request bodies from :func:`load_policies_file`.
    :raises click.ClickException: If the server rejects a policy.
    """
    from omnigent.native.native_terminal import url_component

    for body in policies:
        resp = await client.post(
            f"/v1/sessions/{url_component(session_id)}/policies",
            json=body,
            timeout=30.0,
        )
        if resp.status_code == 409:
            click.echo(
                f"Policy {body['name']!r} is already attached to this session; keeping it.",
                err=True,
            )
            continue
        if resp.status_code >= 400:
            raise click.ClickException(
                f"Could not attach policy {body['name']!r} ({resp.status_code}): {resp.text[:500]}"
            )
