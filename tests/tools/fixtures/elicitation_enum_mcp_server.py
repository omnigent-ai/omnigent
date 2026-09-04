"""Stdio MCP server used by :mod:`tests.e2e.test_mcp_elicitation_user_answer`.

Exposes a single ``deploy`` tool that, when called, sends an
``elicitation/create`` to the client asking which environment to
deploy to.  The schema is::

    {
      "type": "object",
      "properties": {
        "answer": {"type": "string", "enum": ["dev", "staging", "prod"]}
      }
    }

The tool returns the answer it received from the elicitation result so
the test can assert the correct value reached the server.

This is the exact shape that trips the bug: an enum with three
values and NO "allow" entry, so the auto-fill path in
``_build_accept_content`` returns the first value ("dev") instead of
the one the user actually picked.  When the bug is present the tool
returns ``"elicit_answer:dev"`` regardless of what the user chose.
When the bug is fixed it returns the actual selection.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field

mcp = FastMCP("elicitation-enum-test")


class _DeployTarget(BaseModel):
    """Schema for the deploy-target elicitation.

    The field stays annotated as ``str`` (the MCP SDK's elicitation
    validator only allows primitive annotations) and carries the enum via
    ``json_schema_extra``, producing the exact requestedSchema shape from
    the bug report: ``{"type": "string", "enum": ["dev", "staging", "prod"]}``.
    """

    answer: str = Field(json_schema_extra={"enum": ["dev", "staging", "prod"]})


@mcp.tool()
async def deploy(ctx: Context) -> str:
    """Ask the user which environment to deploy to and return the answer.

    :param ctx: FastMCP context, used to call ``ctx.elicit``.
    :returns: ``f"elicit_answer:{answer}"`` where *answer* is the value
        returned by the elicitation.  Returns ``"elicit_answer:declined"``
        when the user declines, ``"elicit_answer:none"`` when
        ``result.data`` is unexpectedly absent.
    """
    result = await ctx.elicit(
        message="Which environment would you like to deploy to?",
        schema=_DeployTarget,
    )
    if result.action != "accept":
        return "elicit_answer:declined"
    if result.data is None:
        return "elicit_answer:none"
    return f"elicit_answer:{result.data.answer}"


if __name__ == "__main__":
    mcp.run()
