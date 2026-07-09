"""calculate fixture tool — safe basic-arithmetic evaluation."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def calculate(expression: str) -> str:
    """
    Safely evaluate a basic arithmetic expression and return the result.

    Only supports basic arithmetic (digits and ``+-*/().%`` and spaces) for
    safety; other characters are rejected.

    :param expression: Arithmetic expression, e.g. ``"6 + 6"``.
    :returns: The result as a string, e.g. ``"12"``, or an error string.
    """
    allowed = set("0123456789+-*/().% ")
    if not all(c in allowed for c in expression):
        return (
            "Error: expression contains disallowed characters. Only basic arithmetic is supported."
        )
    try:
        result = eval(expression, {"__builtins__": {}}, {})
    except Exception as exc:
        return f"Error evaluating '{expression}': {exc}"
    return str(result)
