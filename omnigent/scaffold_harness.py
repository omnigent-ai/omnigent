"""Developer scaffold generator for new Omnigent harnesses.

Generates the boilerplate a new harness needs and prints the
extension-point checklist, so adding a harness starts from a working
skeleton rather than copy-paste. Pairs with the unified
:class:`~omnigent.runtime.harness_descriptors.HarnessDescriptor` registry —
the generated descriptor stub is the single registration the scattered
views derive from.

Usage::

    python -m omnigent.scaffold_harness foo-native \\
        --family native-server --transport http-sse

This prints the files it would create (dry-run by default) or writes them
under the repo with ``--write``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Extension-point checklist (Appendix A of the design). Printed after a
# scaffold so the author knows what still needs hand-wiring.
EXTENSION_CHECKLIST: tuple[str, ...] = (
    "Add the HarnessDescriptor to omnigent/runtime/harness_descriptors.py "
    "(the scattered registries derive from it).",
    "Implement the Executor (or NativeServerTransport) for the harness.",
    "Add fake transport/client tests under tests/.",
    "Add a built-in wrapper spec + server seeding if terminal-first.",
    "Add the ap-web native registry entry if it has a native UI.",
    "Run the harness conformance suite (tests/harness_conformance).",
)


def _module_name(name: str) -> str:
    """
    Convert a harness id to its python module base name.

    :param name: Harness id, e.g. ``"foo-native"``.
    :returns: Module base name, e.g. ``"foo_native"``.
    """
    return name.replace("-", "_")


def _class_name(name: str) -> str:
    """
    Convert a harness id to a PascalCase class prefix.

    :param name: Harness id, e.g. ``"foo-native"``.
    :returns: Class prefix, e.g. ``"FooNative"``.
    """
    return "".join(part.capitalize() for part in name.replace("_", "-").split("-"))


def generate_harness_scaffold(
    name: str,
    *,
    family: str = "native-server",
    transport: str = "http-sse",
) -> dict[str, str]:
    """
    Build the scaffold file map for a new harness.

    :param name: Harness id, e.g. ``"foo-native"``.
    :param family: Descriptor family, e.g. ``"native-server"``.
    :param transport: Transport kind tag, e.g. ``"http-sse"``.
    :returns: Mapping of repo-relative path to file content.
    """
    mod = _module_name(name)
    cls = _class_name(name)
    descriptor_stub = (
        f'    "{name}": HarnessDescriptor(\n'
        f'        id="{name}",\n'
        f'        display_name="{cls}",\n'
        f'        module="omnigent.inner.{mod}_harness",\n'
        f'        family="{family}",\n'
        f'        transport_kind="{transport}",\n'
        f"    ),\n"
    )
    harness_py = (
        f'"""``harness: {name}`` wrap."""\n\n'
        "from __future__ import annotations\n\n"
        "from fastapi import FastAPI\n\n"
        "from omnigent.inner.executor import Executor\n"
        f"from omnigent.inner.{mod}_executor import {cls}Executor\n"
        "from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter\n\n\n"
        f"def _build_{mod}_executor() -> Executor:\n"
        f'    """Construct the {name} executor."""\n'
        f"    return {cls}Executor()\n\n\n"
        "def create_app() -> FastAPI:\n"
        f'    """Build the ``{name}`` harness FastAPI app."""\n'
        f"    adapter = ExecutorAdapter(executor_factory=_build_{mod}_executor)\n"
        "    return adapter.build()\n"
    )
    executor_py = (
        f'"""Executor for the ``{name}`` harness."""\n\n'
        "from __future__ import annotations\n\n"
        "from collections.abc import AsyncIterator\n\n"
        "from omnigent.inner.executor import (\n"
        "    Executor,\n"
        "    ExecutorConfig,\n"
        "    ExecutorEvent,\n"
        "    Message,\n"
        "    ToolSpec,\n"
        "    TurnComplete,\n"
        ")\n\n\n"
        f"class {cls}Executor(Executor):\n"
        f'    """TODO: implement the {name} executor."""\n\n'
        "    async def run_turn(\n"
        "        self,\n"
        "        messages: list[Message],\n"
        "        tools: list[ToolSpec],\n"
        "        system_prompt: str,\n"
        "        config: ExecutorConfig | None = None,\n"
        "    ) -> AsyncIterator[ExecutorEvent]:\n"
        "        del messages, tools, system_prompt, config\n"
        "        raise NotImplementedError\n"
        "        yield TurnComplete(response=None)  # pragma: no cover\n"
    )
    test_py = (
        f'"""Tests for the {name} harness scaffold."""\n\n'
        "from __future__ import annotations\n\n"
        f"from omnigent.inner.{mod}_harness import create_app\n\n\n"
        "def test_create_app() -> None:\n"
        "    assert create_app() is not None\n"
    )
    return {
        f"omnigent/inner/{mod}_harness.py": harness_py,
        f"omnigent/inner/{mod}_executor.py": executor_py,
        f"tests/test_{mod}_harness.py": test_py,
        f"# descriptor stub for {name} (paste into HARNESS_DESCRIPTORS)": descriptor_stub,
    }


def main(argv: list[str] | None = None) -> int:
    """
    CLI entry point for the harness scaffold generator.

    :param argv: Argument vector; ``None`` uses ``sys.argv``.
    :returns: Process exit code.
    """
    parser = argparse.ArgumentParser(prog="omnigent.scaffold_harness")
    parser.add_argument("name", help="Harness id, e.g. foo-native")
    parser.add_argument("--family", default="native-server")
    parser.add_argument("--transport", default="http-sse")
    parser.add_argument("--write", action="store_true", help="Write files (default: dry-run)")
    parser.add_argument("--root", default=".", help="Repo root for --write")
    args = parser.parse_args(argv)

    files = generate_harness_scaffold(args.name, family=args.family, transport=args.transport)
    for path, content in files.items():
        if path.startswith("#"):
            print(f"\n# --- descriptor stub ---\n{content}")
            continue
        if args.write:
            target = Path(args.root) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(f"\n# === {path} ===\n{content}")

    print("\nExtension-point checklist:")
    for item in EXTENSION_CHECKLIST:
        print(f"  [ ] {item}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
