"""calculate fixture tool — safe basic-arithmetic evaluation."""

from __future__ import annotations

import ast
import operator

from omnigent_client.tools import tool

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def _eval_node(node: ast.AST) -> float:
    """Evaluate an arithmetic AST node, rejecting anything non-arithmetic."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        return _BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOPS:
        return _UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise ValueError(f"unsupported expression element: {ast.dump(node)}")


@tool
def calculate(expression: str) -> str:
    """
    Safely evaluate a basic arithmetic expression and return the result.

    Only supports basic arithmetic (numbers and ``+-*/%`` with parentheses);
    anything else is rejected by the AST walker.

    :param expression: Arithmetic expression, e.g. ``"6 + 6"``.
    :returns: The result as a string, e.g. ``"12"``, or an error string.
    """
    try:
        result = _eval_node(ast.parse(expression, mode="eval"))
    except Exception as exc:
        return f"Error evaluating '{expression}': {exc}"
    return str(result)
