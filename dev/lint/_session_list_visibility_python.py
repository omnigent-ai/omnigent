"""Check Python session-list clients with bounded, local AST data flow.

Track query dictionaries in statement order, merge conditional branches, and
follow same-module functions through at most six calls. This is deliberately
not a Python interpreter: imported builders, reflection, arbitrary mutations,
and dynamically assembled endpoint names need an explicit lint suppression.
"""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import parse_qs, urlsplit


class _Unknown(Enum):
    VALUE = 0
    MAYBE_OMITTED = 1


_UNKNOWN = _Unknown.VALUE
_DYNAMIC = "__dynamic__"
_MAX_DEPTH = 6


@dataclass
class _Mapping:
    values: dict[str, _Value] = field(default_factory=dict)


_Value = str | int | bool | None | _Unknown | _Mapping
_Env = dict[str, _Value]
_Function = ast.FunctionDef | ast.AsyncFunctionDef


def _merge(left: _Value, right: _Value) -> _Value:
    if isinstance(left, _Mapping) and isinstance(right, _Mapping):
        return _Mapping(
            {
                key: _merge(value, right.values[key])
                for key, value in left.values.items()
                if key in right.values
            }
        )
    if left == right:
        return left
    if not _explicit(left) or not _explicit(right):
        return _Unknown.MAYBE_OMITTED
    return _UNKNOWN


def _merge_env(left: _Env, right: _Env) -> _Env:
    return {key: _merge(value, right.get(key, _UNKNOWN)) for key, value in left.items()}


def _is_list_url(value: _Value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        path = urlsplit(value).path.rstrip("/")
    except ValueError:
        return False
    return path == "/sessions" or path.endswith("/v1/sessions")


def _explicit(value: _Value) -> bool:
    # An expression-valued visibility is explicit even when its runtime value
    # is unknown. A literal None/empty string demonstrably omits the filter.
    return value is not None and value != "" and value is not _Unknown.MAYBE_OMITTED


def _has_visibility(value: _Value) -> bool:
    if isinstance(value, _Mapping):
        return "visibility" in value.values and _explicit(value.values["visibility"])
    if isinstance(value, str):
        return any(parse_qs(value, keep_blank_values=True).get("visibility", []))
    return False


class _Scanner:
    def __init__(self, tree: ast.Module) -> None:
        self.functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.globals: _Env = {}
        self.hits: set[tuple[int, str]] = set()
        self.active: list[str] = []
        self.cache: dict[tuple[str, str], _Value] = {}

    def run(self, tree: ast.Module) -> list[tuple[int, str]]:
        self.globals, _, _ = self.block(tree.body, self.globals)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                self.function(node, {arg.arg: _UNKNOWN for arg in names})
        return sorted(self.hits)

    def function(self, node: _Function, arguments: _Env) -> _Value:
        key = (str(node.lineno), repr(arguments))
        if key in self.cache:
            return copy.deepcopy(self.cache[key])
        if node.name in self.active or len(self.active) >= _MAX_DEPTH:
            return _UNKNOWN
        self.active.append(node.name)
        env = copy.deepcopy(self.globals)
        positional = [*node.args.posonlyargs, *node.args.args]
        defaults = (
            dict(
                zip(
                    [a.arg for a in positional[-len(node.args.defaults) :]],
                    node.args.defaults,
                    strict=False,
                )
            )
            if node.args.defaults
            else {}
        )
        defaults.update(
            {
                arg.arg: default
                for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults, strict=True)
                if default is not None
            }
        )
        for arg in [*positional, *node.args.kwonlyargs]:
            env[arg.arg] = arguments.get(arg.arg, self.expr(defaults.get(arg.arg), env))
        if node.args.kwarg is not None:
            formal_names = {arg.arg for arg in [*positional, *node.args.kwonlyargs]}
            env[node.args.kwarg.arg] = _Mapping(
                {name: value for name, value in arguments.items() if name not in formal_names}
            )
        _, returns, terminated = self.block(node.body, env)
        if not terminated:
            returns.append(None)
        result = returns[0] if returns else None
        for value in returns[1:]:
            result = _merge(result, value)
        self.active.pop()
        self.cache[key] = copy.deepcopy(result)
        return result

    def expr(self, node: ast.AST | None, env: _Env) -> _Value:
        if node is None:
            return _UNKNOWN
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, (str, int, bool, type(None))) else _UNKNOWN
        if isinstance(node, ast.Name):
            return env.get(node.id, _UNKNOWN)
        if isinstance(node, ast.Await):
            return self.expr(node.value, env)
        if isinstance(node, ast.JoinedStr):
            parts = []
            for part in node.values:
                value = self.expr(
                    part.value if isinstance(part, ast.FormattedValue) else part, env
                )
                parts.append(str(value) if isinstance(value, (str, int)) else _DYNAMIC)
            return "".join(parts)
        if isinstance(node, ast.Dict):
            result = _Mapping()
            for key, value in zip(node.keys, node.values, strict=True):
                resolved = self.expr(value, env)
                if key is None and isinstance(resolved, _Mapping):
                    result.values.update(resolved.values)
                elif isinstance(name := self.expr(key, env), str):
                    result.values[name] = resolved
            return result
        if isinstance(node, ast.Subscript):
            value, key = self.expr(node.value, env), self.expr(node.slice, env)
            return (
                value.values.get(key, _UNKNOWN)
                if isinstance(value, _Mapping) and isinstance(key, str)
                else _UNKNOWN
            )
        if isinstance(node, ast.BinOp):
            left, right = self.expr(node.left, env), self.expr(node.right, env)
            if (
                isinstance(node.op, ast.BitOr)
                and isinstance(left, _Mapping)
                and isinstance(right, _Mapping)
            ):
                return _Mapping(left.values | right.values)
            if isinstance(node.op, ast.Add) and isinstance(left, str) and isinstance(right, str):
                return left + right
            if isinstance(node.op, ast.Add) and isinstance(left, str) and right is _UNKNOWN:
                return left + _DYNAMIC
            if isinstance(node.op, ast.Add) and left is _UNKNOWN and isinstance(right, str):
                return _DYNAMIC + right
            return _UNKNOWN
        if isinstance(node, ast.IfExp):
            test = self.expr(node.test, env)
            if isinstance(test, bool):
                return self.expr(node.body if test else node.orelse, env)
            return _merge(self.expr(node.body, env), self.expr(node.orelse, env))
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = self.expr(node.left, env), self.expr(node.comparators[0], env)
            if not isinstance(left, _Unknown) and not isinstance(right, _Unknown):
                if isinstance(node.ops[0], (ast.Eq, ast.Is)):
                    return left == right
                if isinstance(node.ops[0], (ast.NotEq, ast.IsNot)):
                    return left != right
        if isinstance(node, ast.Call):
            return self.call(node, env)
        # Inspect nested calls even when the enclosing expression is opaque.
        for child in ast.iter_child_nodes(node):
            self.expr(child, env)
        return _UNKNOWN

    def call(self, node: ast.Call, env: _Env) -> _Value:
        args = [self.expr(arg, env) for arg in node.args]
        kwargs: _Env = {}
        for keyword in node.keywords:
            value = self.expr(keyword.value, env)
            if keyword.arg is not None:
                kwargs[keyword.arg] = value
            elif isinstance(value, _Mapping):
                kwargs.update(value.values)
        name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else ""
        )
        receiver = (
            self.expr(node.func.value, env) if isinstance(node.func, ast.Attribute) else _UNKNOWN
        )
        if isinstance(receiver, _Mapping):
            if name == "copy":
                return _Mapping(receiver.values.copy())
            if name == "update":
                for value in args:
                    if isinstance(value, _Mapping):
                        receiver.values.update(value.values)
                receiver.values.update(kwargs)
                return None
            if name in {"pop", "get", "setdefault"} and args and isinstance(args[0], str):
                key = args[0]
                if name == "pop":
                    return receiver.values.pop(key, _UNKNOWN)
                if name == "setdefault":
                    return receiver.values.setdefault(key, args[1] if len(args) > 1 else None)
                return receiver.values.get(key, args[1] if len(args) > 1 else _UNKNOWN)
            if name == "clear":
                receiver.values.clear()
                return None
        if name == "dict":
            values = args[0].values.copy() if args and isinstance(args[0], _Mapping) else {}
            return _Mapping(values | kwargs)
        if (
            isinstance(node.func, ast.Attribute)
            and name == "list"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "sessions"
        ):
            if not _has_visibility(_Mapping(kwargs)):
                self.hits.add(
                    (node.lineno, "sessions.list requires an explicit visibility= argument")
                )
        self.http_call(node, name, args, kwargs)
        if isinstance(node.func, ast.Name) and name in self.functions:
            function = self.functions[name]
            positional = [*function.args.posonlyargs, *function.args.args]
            bound = {arg.arg: value for arg, value in zip(positional, args, strict=False)}
            return self.function(function, bound | kwargs)
        return _UNKNOWN

    def http_call(self, node: ast.Call, name: str, args: list[_Value], kwargs: _Env) -> None:
        lower = name.lower().strip("_")
        is_get = lower in {"get", "get_json", "urlopen"} or lower.endswith("_get")
        is_request = lower in {"request", "host_http_json", "build_request"}
        if not is_get and not is_request:
            return
        if is_request:
            method = kwargs.get("method", args[0] if args else _UNKNOWN)
            if name == "Request" and args and _is_list_url(args[0]) and "method" not in kwargs:
                data = kwargs.get("data", args[1] if len(args) > 1 else None)
                method = "GET" if data is None else "POST"
            if not isinstance(method, str) or method.upper() != "GET":
                return
        urls = [
            value
            for value in [kwargs.get("url"), kwargs.get("path"), *args]
            if _is_list_url(value)
        ]
        for url in urls:
            assert isinstance(url, str)
            # httpx params replace the URL query, including an empty mapping.
            params = kwargs.get("params")
            query = params if params is not None else urlsplit(url).query
            if not _has_visibility(query):
                self.hits.add(
                    (
                        node.lineno,
                        "GET /sessions requires explicit visibility in its query; "
                        "make params visible to this lint or document an inline suppression",
                    )
                )

    def assign(self, target: ast.expr, value: _Value, env: _Env) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, ast.Subscript):
            mapping, key = self.expr(target.value, env), self.expr(target.slice, env)
            if isinstance(mapping, _Mapping) and isinstance(key, str):
                mapping.values[key] = value

    def block(self, body: list[ast.stmt], env: _Env) -> tuple[_Env, list[_Value], bool]:
        returns: list[_Value] = []
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = self.expr(node.value, env)
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    self.assign(target, value, env)
            elif isinstance(node, ast.AugAssign):
                left, right = self.expr(node.target, env), self.expr(node.value, env)
                if (
                    isinstance(node.op, ast.BitOr)
                    and isinstance(left, _Mapping)
                    and isinstance(right, _Mapping)
                ):
                    left.values.update(right.values)
                else:
                    self.assign(node.target, _UNKNOWN, env)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    if isinstance(target, ast.Subscript):
                        mapping, key = self.expr(target.value, env), self.expr(target.slice, env)
                        if isinstance(mapping, _Mapping) and isinstance(key, str):
                            mapping.values.pop(key, None)
            elif isinstance(node, ast.Return):
                returns.append(self.expr(node.value, env) if node.value else None)
                return env, returns, True
            elif isinstance(node, ast.Raise):
                self.expr(node.exc, env)
                return env, returns, True
            elif isinstance(node, ast.If):
                test = self.expr(node.test, env)
                if isinstance(test, (bool, str, int)):
                    env, values, done = self.block(node.body if test else node.orelse, env)
                else:
                    left, left_returns, left_done = self.block(node.body, copy.deepcopy(env))
                    right, right_returns, right_done = self.block(node.orelse, copy.deepcopy(env))
                    env = right if left_done else left if right_done else _merge_env(left, right)
                    values, done = left_returns + right_returns, left_done and right_done
                returns.extend(values)
                if done:
                    return env, returns, True
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    value = self.expr(item.context_expr, env)
                    if item.optional_vars is not None:
                        self.assign(item.optional_vars, value, env)
                env, values, done = self.block(node.body, env)
                returns.extend(values)
                if done:
                    return env, returns, True
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                self.expr(node.test if isinstance(node, ast.While) else node.iter, env)
                before = copy.deepcopy(env)
                env, values, _ = self.block(node.body, env)
                returns.extend(values)
                env = _merge_env(before, env)
                env, values, _ = self.block(node.orelse, env)
                returns.extend(values)
            elif isinstance(node, (ast.Try, ast.TryStar)):
                before = copy.deepcopy(env)
                env, values, done = self.block(node.body, env)
                returns.extend(values)
                if not done:
                    env, values, done = self.block(node.orelse, env)
                    returns.extend(values)
                survivors = [] if done else [env]
                for handler in node.handlers:
                    branch, values, handler_done = self.block(handler.body, copy.deepcopy(before))
                    returns.extend(values)
                    if not handler_done:
                        survivors.append(branch)
                if survivors:
                    env = survivors[0]
                    for branch in survivors[1:]:
                        env = _merge_env(env, branch)
                env, values, finally_done = self.block(node.finalbody, env)
                returns.extend(values)
                if finally_done or not survivors:
                    return env, returns, True
            else:
                for child in ast.iter_child_nodes(node):
                    self.expr(child, env)
        return env, returns, False


def scan(source: str) -> list[tuple[int, str]]:
    """Return line numbers and messages for client calls with implicit visibility."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return _Scanner(tree).run(tree)
