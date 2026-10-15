"""Read the facts the referee checks answers against, from one installed build.

Run with the build's own interpreter, in isolated mode, so the facts come
from the code under test rather than from this checkout::

    <venv>/bin/python -I referee_probe.py surface
    <venv>/bin/python -I referee_probe.py yaml < snippets.json

``surface`` prints the CLI tree, tool names, harness names, and the
``OMNIGENT_*`` environment variables the package reads. ``yaml`` loads agent
configs through the build's own loaders and reports, for each snippet, the
load error (if any) and every key the parser never read.

Only the standard library, PyYAML, and the installed ``omnigent`` are imported.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml

_ENV_VAR_RE = re.compile(r"\bOMNIGENT_[A-Z0-9_]*[A-Z0-9]\b")
_LITERAL_RE = re.compile(r"(?<![\w])[\"']([A-Za-z_][A-Za-z0-9_-]{1,60})[\"']")


def surface() -> dict[str, Any]:
    """Facts that do not depend on an answer: CLI, tools, harnesses, env vars."""
    import click

    import omnigent
    from omnigent.cli import cli
    from omnigent.version import VERSION

    def walk(command: click.Command, path: list[str]) -> dict[str, Any]:
        ctx = click.Context(command, info_name=path[-1] if path else "omnigent")
        settings = command.context_settings or {}
        options: list[str] = []
        flags: list[str] = []
        for param in command.params:
            if isinstance(param, click.Option):
                names = [*param.opts, *param.secondary_opts]
                options.extend(names)
                if param.is_flag or param.count:
                    flags.extend(names)
        options.extend(command.get_help_option_names(ctx))
        node: dict[str, Any] = {
            "hidden": bool(command.hidden),
            "options": sorted(set(options)),
            "flags": sorted(set(flags)),
            "arguments": [p.name for p in command.params if isinstance(p, click.Argument)],
            "passthrough": bool(
                settings.get("ignore_unknown_options", command.ignore_unknown_options)
            ),
            "extra_args": bool(settings.get("allow_extra_args", command.allow_extra_args)),
            "help": (command.short_help or (command.help or "").strip().split("\n")[0])[:200],
            "commands": {},
        }
        if isinstance(command, click.Group):
            for name in command.list_commands(ctx):
                sub = command.get_command(ctx, name)
                if sub is not None:
                    node["commands"][name] = walk(sub, [*path, name])
        return node

    package_dir = Path(omnigent.__file__).resolve().parent
    env_vars: set[str] = set()
    literals: set[str] = set()
    for source in package_dir.rglob("*.py"):
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        env_vars.update(_ENV_VAR_RE.findall(text))
        literals.update(_LITERAL_RE.findall(text))

    import omnigent.cli as cli_module

    facts: dict[str, Any] = {
        "version": VERSION,
        "package_dir": str(package_dir),
        "cli": walk(cli, []),
        "env_vars": sorted(env_vars),
        # Names some reader uses, for config files that have no schema.
        "string_literals": sorted(literals),
        # Keys `omnigent config set/unset` accepts.
        "config_set_keys": sorted(getattr(cli_module, "_GLOBAL_CONFIG_KEYS", ())),
    }
    facts.update(_tool_names())
    facts.update(_harness_names())
    # Every key the loaders look up at the top level, present or not.
    single = "name: probe\nprompt: probe\nexecutor:\n  harness: claude-native\n"
    bundle = "spec_version: 1\n" + single.replace(
        "  harness:", "  type: omnigent\n  config:\n    harness:"
    )
    root_keys: set[str] = set()
    for text, single_file in ((single, True), (bundle, False)):
        root_keys |= set(check_config(text, single_file=single_file)["root_read"])
    facts["spec_root_keys"] = sorted(root_keys)
    return facts


def _tool_names() -> dict[str, Any]:
    import inspect

    from omnigent.tools.builtins import BUILTIN_NAMES, INSTANTIABLE_BUILTINS
    from omnigent.tools.manager import ToolManager

    # A few builtins are built by ToolManager itself rather than the registry.
    special = set(re.findall(r'name == "(\w+)"', inspect.getsource(ToolManager._create_builtin)))
    names = set(BUILTIN_NAMES)
    try:
        from omnigent.runner.tool_dispatch import (
            _NATIVE_RELAY_BUILTIN_TOOLS,
            build_native_relay_tool_schemas,
        )

        names |= set(_NATIVE_RELAY_BUILTIN_TOOLS)
        names |= {schema["name"] for schema in build_native_relay_tool_schemas(None)}
    except ImportError:
        pass
    return {"tools": sorted(names), "spec_builtins": sorted(set(INSTANTIABLE_BUILTINS) | special)}


def _harness_names() -> dict[str, Any]:
    from omnigent import harness_plugins

    accepted = set(harness_plugins.accepted_harnesses())
    accepted |= set(harness_plugins.harness_aliases())
    return {"harnesses": sorted(accepted)}


# ── Key tracking ────────────────────────────────────────────────────────


class _Node:
    """Read record shared by a mapping and every copy made of it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.read: set[str] = set()
        self.iterated = False


class _TrackingDict(dict):
    """A dict that records which keys the parser asks for."""

    _node: _Node

    def get(self, key: Any, default: Any = None) -> Any:
        self._node.read.add(str(key))
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self._node.read.add(str(key))
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._node.read.add(str(key))
        return super().__contains__(key)

    def pop(self, key: Any, *default: Any) -> Any:
        self._node.read.add(str(key))
        return super().pop(key, *default)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._node.read.add(str(key))
        return super().setdefault(key, default)

    # Iterating hands every key to the caller: a name map or a copy. Either
    # way the parser saw the keys, so they are not reported as unread.
    def __iter__(self):  # type: ignore[no-untyped-def]
        self._node.iterated = True
        return super().__iter__()

    def keys(self):  # type: ignore[no-untyped-def]
        self._node.iterated = True
        return super().keys()

    def items(self):  # type: ignore[no-untyped-def]
        self._node.iterated = True
        return super().items()

    def values(self):  # type: ignore[no-untyped-def]
        self._node.iterated = True
        return super().values()

    def copy(self) -> _TrackingDict:
        return self.__copy__()

    def __copy__(self) -> _TrackingDict:
        clone = _TrackingDict(dict.items(self))
        clone._node = self._node
        return clone

    def __deepcopy__(self, memo: dict[int, Any]) -> _TrackingDict:
        import copy

        clone = _TrackingDict()
        clone._node = self._node
        memo[id(self)] = clone
        for key, value in dict.items(self):
            dict.__setitem__(clone, copy.deepcopy(key, memo), copy.deepcopy(value, memo))
        return clone


def _tracking_loader(base: type) -> tuple[type, list[_TrackingDict]]:
    """A subclass of *base* whose mappings are tracking dicts."""
    made: list[_TrackingDict] = []

    class Loader(base):  # type: ignore[misc, valid-type]
        pass

    def construct(loader: Any, node: Any) -> _TrackingDict:
        mapping = _TrackingDict()
        mapping._node = _Node("")
        made.append(mapping)
        yield mapping
        loader.flatten_mapping(node)
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=True)
            dict.__setitem__(mapping, key, loader.construct_object(value_node, deep=True))

    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct)
    return Loader, made


def _unread(root: Any, path: str = "") -> list[str]:
    """Dotted paths of keys under *root* that no reader asked for."""
    found: list[str] = []
    if isinstance(root, _TrackingDict):
        node = root._node
        for key, value in dict.items(root):
            child = f"{path}.{key}" if path else str(key)
            if not node.iterated and str(key) not in node.read:
                found.append(child)
                continue
            found.extend(_unread(value, child))
    elif isinstance(root, list):
        for index, item in enumerate(root):
            found.extend(_unread(item, f"{path}[{index}]"))
    return found


def check_config(
    text: str | None, *, single_file: bool, path: str | None = None
) -> dict[str, Any]:
    """
    Load one agent config the way ``omnigent run`` does and report problems.

    :param text: Complete YAML for a single-file agent (``single_file``) or a
        bundle ``config.yaml``. Ignored when *path* is given.
    :param single_file: Which loader path to use for *text*.
    :param path: An agent file or directory on disk to load as-is.
    :returns: ``{"error": str | None, "unread": [dotted paths]}``.
    """
    import omnigent.inner.loader as inner_loader
    import omnigent.spec.parser as spec_parser
    from omnigent.spec import load

    patched: list[tuple[Any, str, Any]] = []
    roots: list[_TrackingDict] = []
    for module, attr in (
        (spec_parser, "_ConfigYamlLoader"),
        (inner_loader, "_OmnigentYamlLoader"),
    ):
        original = getattr(module, attr)
        loader, made = _tracking_loader(original)
        roots.append(made)  # type: ignore[arg-type]
        patched.append((module, attr, original))
        setattr(module, attr, loader)
    error: str | None = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            if path is not None:
                load(Path(path), expand_env=False)
            elif single_file:
                target = Path(tmp) / "agent.yaml"
                target.write_text(text, encoding="utf-8")
            else:
                target = Path(tmp)
                (target / "config.yaml").write_text(text, encoding="utf-8")
            load(target, expand_env=False)
    except Exception as exc:  # noqa: BLE001 — the load error is the finding
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        for module, attr, original in patched:
            setattr(module, attr, original)
    # The first mapping each loader built is the document root.
    unread: list[str] = []
    root_read: set[str] = set()
    for made in roots:
        if made:
            unread.extend(_unread(made[0]))  # type: ignore[index]
            root_read |= made[0]._node.read  # type: ignore[index]
    return {"error": error, "unread": sorted(set(unread)), "root_read": sorted(root_read)}


def yaml_checks(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run :func:`check_config` for each ``{"text", "single_file"}`` or ``{"path"}`` item."""
    return [
        check_config(
            item.get("text"), single_file=bool(item.get("single_file")), path=item.get("path")
        )
        for item in payload
    ]


def python_refs(payload: list[str]) -> list[bool]:
    """Whether each dotted path (e.g. a policy handler) imports in this build."""
    import importlib

    results = []
    for dotted in payload:
        parts = dotted.split(".")
        found = False
        for split in range(len(parts), 0, -1):
            try:
                target: Any = importlib.import_module(".".join(parts[:split]))
            except Exception:  # noqa: BLE001 — any import failure means "not here"
                continue
            try:
                for attr in parts[split:]:
                    target = getattr(target, attr)
            except AttributeError:
                break
            found = True
            break
        results.append(found)
    return results


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    if command == "surface":
        json.dump(surface(), sys.stdout)
        return 0
    if command == "yaml":
        json.dump(yaml_checks(json.load(sys.stdin)), sys.stdout)
        return 0
    if command == "pyrefs":
        json.dump(python_refs(json.load(sys.stdin)), sys.stdout)
        return 0
    print("usage: referee_probe.py surface | yaml | pyrefs < input.json", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
