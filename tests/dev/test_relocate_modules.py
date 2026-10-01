"""Tests for dev/refactor/relocate_modules.py.

All tests operate on throwaway git repositories created under pytest's
tmp_path fixture — the real repository tree is never touched.

Set RELOCATE_SKIP_FORMAT=1 (done automatically via the module-level
fixture) so ruff is not required for the test suite to pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# Skip ruff post-format in every test; individual tests may unset the variable.
os.environ["RELOCATE_SKIP_FORMAT"] = "1"

from dev.refactor.relocate_modules import (
    AliasEntry,
    MoveEntry,
    build_text_replacer,
    cmd_apply,
    cmd_check,
    cmd_rewrite,
    convert_relative_imports,
    expand_rename_map,
    find_warnings,
    generate_stub,
    is_regular_file,
    module_to_path,
    path_to_module,
    rewrite_absolute_imports,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def make_repo(tmp_path: Path) -> Path:
    """Initialise a minimal git repo in *tmp_path* and return its path."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def write_file(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))
    return path


def commit_all(root: Path, msg: str = "initial") -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", msg)


def make_args(**kwargs):  # type: ignore[return]
    """Create a simple namespace for passing to cmd_* functions."""
    import argparse

    ns = argparse.Namespace()
    defaults = {
        "repo": ".",
        "dry_run": False,
        "report": None,
        "paths": [],
    }
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


# ---------------------------------------------------------------------------
# Unit tests: utilities
# ---------------------------------------------------------------------------


def test_module_to_path_file(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/foo.py", "x = 1\n")
    commit_all(root)
    p = module_to_path("pkg.foo", root)
    assert p == root / "pkg" / "foo.py"


def test_module_to_path_package(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    commit_all(root)
    p = module_to_path("pkg", root)
    assert p == root / "pkg"


def test_module_to_path_missing(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, ".gitkeep", "")  # need at least one file for git commit
    commit_all(root)
    assert module_to_path("does.not.exist", root) is None


def test_path_to_module_file(tmp_path):
    root = make_repo(tmp_path)
    assert path_to_module(root / "pkg" / "foo.py", root) == "pkg.foo"


def test_path_to_module_init(tmp_path):
    root = make_repo(tmp_path)
    assert path_to_module(root / "pkg" / "__init__.py", root) == "pkg"


def test_path_to_module_non_py(tmp_path):
    root = make_repo(tmp_path)
    assert path_to_module(root / "pkg" / "foo.txt", root) is None


# ---------------------------------------------------------------------------
# Unit tests: expand_rename_map
# ---------------------------------------------------------------------------


def test_expand_rename_map_module(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/old_mod.py", "x = 1\n")
    commit_all(root)
    moves = [MoveEntry(old="pkg.old_mod", new="pkg.new_mod")]
    rmap, pkgs = expand_rename_map(moves, root)
    assert rmap == {"pkg.old_mod": "pkg.new_mod"}
    assert pkgs == set()


def test_expand_rename_map_package(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/inner/__init__.py", "")
    write_file(root, "pkg/inner/foo.py", "")
    write_file(root, "pkg/inner/bar.py", "")
    commit_all(root)
    moves = [MoveEntry(old="pkg.inner", new="pkg.new_inner")]
    rmap, pkgs = expand_rename_map(moves, root)
    assert rmap.get("pkg.inner") == "pkg.new_inner"
    assert rmap.get("pkg.inner.foo") == "pkg.new_inner.foo"
    assert rmap.get("pkg.inner.bar") == "pkg.new_inner.bar"
    assert "pkg.inner" in pkgs


# ---------------------------------------------------------------------------
# Unit tests: text replacer
# ---------------------------------------------------------------------------


def test_text_replacer_basic():
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    result = finder.sub(callback, "import pkg.old")
    assert result == "import pkg.new"


def test_text_replacer_boundary_negative():
    """pkg.old_extra must NOT be rewritten by the pkg.old rule."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "import pkg.old_extra"
    result = finder.sub(callback, text)
    assert result == "import pkg.old_extra"


def test_text_replacer_prefix_boundary():
    """foo.pkg.old must NOT be rewritten (left boundary excludes preceding .)."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "foo.pkg.old"
    result = finder.sub(callback, text)
    assert result == "foo.pkg.old"


def test_text_replacer_oldx_not_matched():
    """pkg.oldx should not be matched by a pkg.old rule."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "pkg.oldx"
    result = finder.sub(callback, text)
    assert result == "pkg.oldx"


def test_text_replacer_no_file_extension_match():
    """pkg.old.py should not be matched as a dotted name (it's a file path)."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "see pkg.old.py for details"
    result = finder.sub(callback, text)
    # pkg.old.py should NOT be rewritten by the dotted rule
    assert "pkg.old.py" in result


def test_text_replacer_dotted_tail_carried():
    """pkg.old.SomeClass should become pkg.new.SomeClass."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "pkg.old.SomeClass"
    result = finder.sub(callback, text)
    assert result == "pkg.new.SomeClass"


def test_text_replacer_path_rewrite():
    """old/path.py should be rewritten to new/path.py."""
    rename_map = {"pkg.old.mod": "pkg.new.mod"}
    finder, callback = build_text_replacer(rename_map, set())
    text = '"pkg/old/mod.py"'
    result = finder.sub(callback, text)
    assert result == '"pkg/new/mod.py"'


def test_text_replacer_config_yaml_untouched():
    """pkg/config.yaml must not be rewritten by a pkg/config rule."""
    rename_map = {"pkg.config": "pkg.settings"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "~/.omnigent/config.yaml"
    result = finder.sub(callback, text)
    assert result == "~/.omnigent/config.yaml"


def test_text_replacer_longer_wins():
    """When cli and cli_config both exist, cli_config rule wins for cli_config."""
    rename_map = {
        "pkg.cli": "pkg.cli.commands",
        "pkg.cli_config": "pkg.cli.config_commands",
    }
    finder, callback = build_text_replacer(rename_map, set())
    text = "import pkg.cli_config"
    result = finder.sub(callback, text)
    assert result == "import pkg.cli.config_commands"


def test_text_replacer_colon_attr():
    """Colon-style references like "pkg.old:attr" should be rewritten."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = '"pkg.old:MyClass"'
    result = finder.sub(callback, text)
    assert result == '"pkg.new:MyClass"'


def test_text_replacer_patch_target():
    """unittest.mock.patch target strings should be rewritten."""
    rename_map = {"pkg.old.mod": "pkg.new.mod"}
    finder, callback = build_text_replacer(rename_map, set())
    text = 'patch("pkg.old.mod.fn")'
    result = finder.sub(callback, text)
    assert result == 'patch("pkg.new.mod.fn")'


def test_text_replacer_markdown_path():
    """Markdown code blocks referencing paths should be rewritten."""
    rename_map = {"pkg.inner.foo": "pkg.harnesses.foo.executor"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "See `pkg/inner/foo.py` for details."
    result = finder.sub(callback, text)
    assert "pkg/harnesses/foo/executor.py" in result


def test_text_replacer_package_dir():
    """Package directory references should be rewritten."""
    rename_map = {"pkg.inner": "pkg.harnesses"}
    finder, callback = build_text_replacer(rename_map, {"pkg.inner"})
    text = "see pkg/inner/ for more"
    result = finder.sub(callback, text)
    assert "pkg/harnesses/" in result


# ---------------------------------------------------------------------------
# Unit tests: relative import conversion
# ---------------------------------------------------------------------------


def test_convert_relative_imports_dot(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/sub.py", "")
    write_file(root, "pkg/consumer.py", "from .sub import Foo\n")
    commit_all(root)

    rename_map = {"pkg.sub": "other.sub"}
    source = (root / "pkg" / "consumer.py").read_text()
    result = convert_relative_imports(
        source, root / "pkg" / "consumer.py", root, rename_map, convert_all=False
    )
    assert "from pkg.sub import Foo" in result


def test_convert_relative_imports_dot_dot(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/inner/__init__.py", "")
    write_file(root, "pkg/inner/mod.py", "from ..other import Bar\n")
    write_file(root, "pkg/other.py", "")
    commit_all(root)

    rename_map = {"pkg.other": "new.other"}
    source = (root / "pkg" / "inner" / "mod.py").read_text()
    result = convert_relative_imports(
        source, root / "pkg" / "inner" / "mod.py", root, rename_map, convert_all=False
    )
    assert "from pkg.other import Bar" in result


def test_convert_relative_imports_from_dot_import(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/consumer.py", "from . import sub\n")
    write_file(root, "pkg/sub.py", "")
    commit_all(root)

    rename_map = {"pkg.sub": "other.sub"}
    source = (root / "pkg" / "consumer.py").read_text()
    result = convert_relative_imports(
        source, root / "pkg" / "consumer.py", root, rename_map, convert_all=False
    )
    assert "from pkg import sub" in result


def test_convert_relative_imports_not_in_scope(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/consumer.py", "from .unrelated import Foo\n")
    write_file(root, "pkg/unrelated.py", "")
    commit_all(root)

    rename_map = {"pkg.other": "new.other"}  # unrelated not in rename_map
    source = (root / "pkg" / "consumer.py").read_text()
    result = convert_relative_imports(
        source, root / "pkg" / "consumer.py", root, rename_map, convert_all=False
    )
    # Should be unchanged.
    assert result == source


def test_convert_relative_imports_all_for_moved_file(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/other.py", "")
    write_file(root, "pkg/mover.py", "from .other import X\n")
    commit_all(root)

    rename_map: dict[str, str] = {}
    source = (root / "pkg" / "mover.py").read_text()
    result = convert_relative_imports(
        source, root / "pkg" / "mover.py", root, rename_map, convert_all=True
    )
    assert "from pkg.other import X" in result


# ---------------------------------------------------------------------------
# Unit tests: AST import rewrite
# ---------------------------------------------------------------------------


def test_rewrite_absolute_imports_simple():
    src = "from pkg.inner import foo_exec\n"
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses.foo import executor as foo_exec" in result


def test_rewrite_absolute_imports_same_leaf():
    """When new leaf matches the original name, no alias is needed."""
    src = "from pkg.inner import executor\n"
    rename_map = {"pkg.inner.executor": "pkg.harnesses.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses import executor" in result
    assert " as executor" not in result


def test_rewrite_absolute_imports_with_alias():
    src = "from pkg.inner import foo_exec as fe\n"
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses.foo import executor as fe" in result


def test_rewrite_absolute_imports_split_mixed():
    """Mixed moved/non-moved names are split into separate statements."""
    src = "from pkg.inner import foo_exec, keep_this\n"
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses.foo import executor as foo_exec" in result
    assert "from pkg.inner import keep_this" in result


def test_rewrite_absolute_imports_type_checking():
    """Imports inside TYPE_CHECKING blocks are rewritten."""
    src = textwrap.dedent(
        """\
        from __future__ import annotations
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from pkg.inner import foo_exec
        """
    )
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses.foo import executor as foo_exec" in result


def test_rewrite_absolute_imports_function_scope():
    """Imports inside functions are rewritten."""
    src = textwrap.dedent(
        """\
        def load():
            from pkg.inner import foo_exec
            return foo_exec()
        """
    )
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert "from pkg.harnesses.foo import executor as foo_exec" in result


def test_rewrite_absolute_imports_no_change():
    src = "from pkg.inner import keep_this\n"
    rename_map = {"pkg.inner.foo_exec": "pkg.harnesses.foo.executor"}
    result = rewrite_absolute_imports(src, rename_map)
    assert result == src


# ---------------------------------------------------------------------------
# Unit tests: compat stub generation
# ---------------------------------------------------------------------------


def test_generate_stub_with_version():
    alias = AliasEntry(old="pkg.old_name", new="pkg.new.name", removal="0.19.0")
    stub = generate_stub(alias)
    assert "sys.modules[__name__] = _target" in stub
    assert "from pkg.new import name as _target" in stub
    assert "0.19.0" in stub


def test_generate_stub_no_version():
    alias = AliasEntry(old="pkg.old_name", new="pkg.new.name")
    stub = generate_stub(alias)
    assert "sys.modules[__name__] = _target" in stub
    assert "from pkg.new import name as _target" in stub


# ---------------------------------------------------------------------------
# Integration: module move
# ---------------------------------------------------------------------------


def test_apply_module_move(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "VALUE = 42\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    write_file(root, "docs.md", "see pkg/old_mod.py\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    # Old file gone, new file exists.
    assert not (root / "pkg" / "old_mod.py").exists()
    assert (root / "pkg" / "new_mod.py").exists()

    # Consumer import rewritten.
    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.new_mod" in consumer
    assert "pkg.old_mod" not in consumer

    # Markdown rewritten.
    docs = (root / "docs.md").read_text()
    assert "pkg/new_mod.py" in docs


# ---------------------------------------------------------------------------
# Integration: package move with resources
# ---------------------------------------------------------------------------


def test_apply_package_move(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/inner/__init__.py", "")
    write_file(root, "pkg/inner/foo.py", "X = 1\n")
    write_file(root, "pkg/inner/bar.py", "Y = 2\n")
    write_file(root, "pkg/inner/data.txt", "resource data\n")
    write_file(root, "pkg/consumer.py", "from pkg.inner import foo\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.inner"
        new = "pkg.harnesses"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    # Package moved.
    assert not (root / "pkg" / "inner").exists()
    assert (root / "pkg" / "harnesses" / "foo.py").exists()
    assert (root / "pkg" / "harnesses" / "bar.py").exists()
    assert (root / "pkg" / "harnesses" / "data.txt").exists()

    # Consumer rewritten.
    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.harnesses" in consumer
    assert "pkg.inner" not in consumer


# ---------------------------------------------------------------------------
# Integration: file-to-package collision
# ---------------------------------------------------------------------------


def test_apply_file_to_package(tmp_path):
    """cli.py -> cli/commands.py while cli_config.py -> cli/config_commands.py."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/cli.py", "def run(): pass\n")
    write_file(root, "pkg/cli_config.py", "SETTINGS = {}\n")
    write_file(
        root,
        "pkg/main.py",
        "from pkg.cli import run\nfrom pkg.cli_config import SETTINGS\n",
    )
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.cli"
        new = "pkg.cli.commands"

        [[move]]
        old = "pkg.cli_config"
        new = "pkg.cli.config_commands"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    assert (root / "pkg" / "cli" / "commands.py").exists()
    assert (root / "pkg" / "cli" / "config_commands.py").exists()
    assert not (root / "pkg" / "cli_config.py").exists()

    main_src = (root / "pkg" / "main.py").read_text()
    assert "pkg.cli.commands" in main_src or "pkg.cli import" in main_src
    assert "pkg.cli_config" not in main_src


# ---------------------------------------------------------------------------
# Integration: replace = true
# ---------------------------------------------------------------------------


def test_apply_replace_true(tmp_path):
    """replace = true removes the destination before moving."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "VALUE = 1\n")
    write_file(root, "pkg/new_mod.py", "# stub wrapper\n")  # already exists
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        replace = true
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    new_content = (root / "pkg" / "new_mod.py").read_text()
    assert "VALUE = 1" in new_content  # From old_mod.py, not the stub wrapper.


# ---------------------------------------------------------------------------
# Integration: idempotent second apply
# ---------------------------------------------------------------------------


def test_apply_idempotent(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "VALUE = 42\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)
    # Commit the result so re-run can track files.
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "after first apply")

    # Second apply should be a no-op.
    # Re-run apply — should not error.
    cmd_apply(args)


# ---------------------------------------------------------------------------
# Integration: check command
# ---------------------------------------------------------------------------


def test_check_passes_when_clean(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/new_mod.py", "VALUE = 42\n")
    write_file(root, "pkg/consumer.py", "from pkg.new_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_check(args)  # Should not raise.


def test_check_fails_with_old_references(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "VALUE = 42\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    with pytest.raises(SystemExit) as exc:
        cmd_check(args)
    assert exc.value.code == 1


def test_check_excludes_alias_stubs(tmp_path):
    """Alias stubs intentionally reference old names and must be skipped by check."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/new_mod.py", "VALUE = 42\n")
    # Simulate a compat stub at the old path.
    stub_src = textwrap.dedent(
        """\
        \"\"\"pkg.old_mod — compat stub.\"\"\"
        import sys as _sys
        from pkg import new_mod as _target
        _sys.modules[__name__] = _target
        """
    )
    write_file(root, "pkg/old_mod.py", stub_src)
    write_file(root, "pkg/consumer.py", "from pkg.new_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"

        [[alias]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_check(args)  # Should not raise (stub excluded).


# ---------------------------------------------------------------------------
# Integration: rewrite command
# ---------------------------------------------------------------------------


def test_rewrite_command(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_rewrite(args)

    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.new_mod" in consumer
    assert "pkg.old_mod" not in consumer


# ---------------------------------------------------------------------------
# Integration: compat stub created and importable
# ---------------------------------------------------------------------------


def test_alias_stub_created(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_name.py", "VALUE = 99\n")
    write_file(root, "pkg/consumer.py", "import pkg.old_name\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_name"
        new = "pkg.new_name"

        [[alias]]
        old = "pkg.old_name"
        new = "pkg.new_name"
        removal = "0.19.0"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    stub_path = root / "pkg" / "old_name.py"
    assert stub_path.exists(), "Stub not created"
    stub_src = stub_path.read_text()
    assert "sys.modules[__name__] = _target" in stub_src
    assert "0.19.0" in stub_src


def test_alias_stub_importable(tmp_path):
    """Importing through the old name via the stub yields the same module."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_name.py", "VALUE = 99\n")
    write_file(root, "pkg/consumer.py", "import pkg.old_name\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_name"
        new = "pkg.new_name"

        [[alias]]
        old = "pkg.old_name"
        new = "pkg.new_name"
        removal = "0.19.0"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    # Add the repo root to sys.path so we can import the fake package.
    sys.path.insert(0, str(root))
    try:
        # Remove any cached modules.
        for key in list(sys.modules):
            if key.startswith("pkg"):
                del sys.modules[key]

        import pkg.new_name as new_mod
        import pkg.old_name as old_mod

        assert old_mod is new_mod, "Stub should point to the same module object"
        assert old_mod.VALUE == 99
    finally:
        sys.path.remove(str(root))
        for key in list(sys.modules):
            if key.startswith("pkg"):
                del sys.modules[key]


# ---------------------------------------------------------------------------
# Integration: dynamic reference warning
# ---------------------------------------------------------------------------


def test_dynamic_reference_warning(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "X = 1\n")
    consumer_src = (
        "import importlib\n"
        'MOD_NAME = "pkg.old_mod." + some_var\n'
        "mod = importlib.import_module(MOD_NAME)\n"
    )
    write_file(root, "pkg/consumer.py", consumer_src)
    commit_all(root)

    source = (root / "pkg" / "consumer.py").read_text()
    warns = find_warnings(source, root / "pkg" / "consumer.py", {"pkg.old_mod": "pkg.new_mod"})
    assert any("dynamic" in w.lower() or "string concatenation" in w.lower() for w in warns)


# ---------------------------------------------------------------------------
# Integration: cleanup delete_empty
# ---------------------------------------------------------------------------


def test_cleanup_delete_empty(tmp_path):
    """delete_empty removes a package that only has __init__.py left after moves."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/inner/__init__.py", "")
    write_file(root, "pkg/inner/mod.py", "X = 1\n")
    write_file(root, "pkg/consumer.py", "from pkg.inner.mod import X\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.inner.mod"
        new = "pkg.mod"

        [cleanup]
        delete_empty = ["pkg.inner"]
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    # The inner package should be gone.
    assert not (root / "pkg" / "inner").exists()


# ---------------------------------------------------------------------------
# Integration: dry-run makes no changes
# ---------------------------------------------------------------------------


def test_dry_run_no_changes(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "VALUE = 42\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import VALUE\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )

    args = make_args(map=str(map_file), repo=str(root), dry_run=True)
    cmd_apply(args)

    # File should still exist at old location.
    assert (root / "pkg" / "old_mod.py").exists()
    # Consumer should be unchanged.
    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.old_mod" in consumer


# ---------------------------------------------------------------------------
# Integration: exclude patterns
# ---------------------------------------------------------------------------


def test_exclude_patterns(tmp_path):
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "X = 1\n")
    write_file(root, "CHANGELOG.md", "pkg.old_mod was here\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import X\n")
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"

        [cleanup]
        exclude = ["CHANGELOG.md"]
        """,
    )

    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)

    # Changelog should be untouched.
    changelog = (root / "CHANGELOG.md").read_text()
    assert "pkg.old_mod" in changelog

    # Consumer should be rewritten.
    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.new_mod" in consumer


# ---------------------------------------------------------------------------
# is_regular_file helper
# ---------------------------------------------------------------------------


def test_is_regular_file_plain(tmp_path):
    f = tmp_path / "foo.py"
    f.write_text("x = 1")
    assert is_regular_file(f)


def test_is_regular_file_symlink_to_file(tmp_path):
    target = tmp_path / "real.py"
    target.write_text("x = 1")
    link = tmp_path / "link.py"
    link.symlink_to(target)
    assert not is_regular_file(link)


def test_is_regular_file_symlink_to_dir(tmp_path):
    d = tmp_path / "subdir"
    d.mkdir()
    link = tmp_path / "link"
    link.symlink_to(d)
    assert not is_regular_file(link)


def test_is_regular_file_directory(tmp_path):
    assert not is_regular_file(tmp_path)


# ---------------------------------------------------------------------------
# Integration: tracked symlinks are skipped, not crashed on
# ---------------------------------------------------------------------------


def test_apply_skips_symlink_to_dir(tmp_path):
    """apply must not crash on a tracked symlink-to-directory."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "X = 1\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import X\n")
    # Create a symlink to a directory and track it.
    subdir = root / "resources" / "examples"
    subdir.mkdir(parents=True)
    (subdir / "data.txt").write_text("hello\n")
    link = root / "resources" / "examples" / "mylink"
    link.symlink_to(subdir.parent)  # symlink to a directory
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )
    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)  # must not raise IsADirectoryError

    # The actual Python file was still rewritten.
    consumer = (root / "pkg" / "consumer.py").read_text()
    assert "pkg.new_mod" in consumer


def test_apply_skips_symlink_to_file(tmp_path):
    """apply must not crash on a tracked symlink-to-file; the link is skipped."""
    root = make_repo(tmp_path)
    write_file(root, "pkg/__init__.py", "")
    write_file(root, "pkg/old_mod.py", "X = 1\n")
    write_file(root, "pkg/consumer.py", "from pkg.old_mod import X\n")
    # Symlink to a file
    real = root / "resources" / "original.py"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_text("# not a tracked file\n")
    link = root / "resources" / "link_file.py"
    link.symlink_to(real)
    commit_all(root)

    map_file = write_file(
        root,
        "map.toml",
        """\
        [[move]]
        old = "pkg.old_mod"
        new = "pkg.new_mod"
        """,
    )
    args = make_args(map=str(map_file), repo=str(root))
    cmd_apply(args)  # must not crash

    # The symlink is still a symlink (not replaced with a copy).
    assert link.is_symlink()
    # The consumer was rewritten normally.
    assert "pkg.new_mod" in (root / "pkg" / "consumer.py").read_text()


# ---------------------------------------------------------------------------
# Unit tests: overlapping-prefix cases for the fast replacer
# ---------------------------------------------------------------------------


def test_replacer_overlapping_all_in_one_line():
    """pkg.cli, pkg.cli_config, and pkg.cli.sub in a single line."""
    rename_map = {
        "pkg.cli": "pkg.cmds.main",
        "pkg.cli_config": "pkg.cmds.config",
        "pkg.cli.sub": "pkg.cmds.sub",
    }
    finder, callback = build_text_replacer(rename_map, set())
    text = "from pkg.cli import x; from pkg.cli_config import y; from pkg.cli.sub import z"
    result = finder.sub(callback, text)
    # cli_config must not be matched by the cli rule
    assert "pkg.cmds.config" in result
    assert "pkg.cmds.sub" in result
    assert "pkg.cmds.main" in result
    # No leftover old names
    assert "pkg.cli_config" not in result
    assert "pkg.cli.sub" not in result


def test_replacer_longer_wins_over_shorter():
    """When both pkg.cli and pkg.cli.sub are in the map, cli.sub wins for cli.sub."""
    rename_map = {
        "pkg.cli": "pkg.commands",
        "pkg.cli.sub": "pkg.commands.sub_module",
    }
    finder, callback = build_text_replacer(rename_map, set())
    text = "import pkg.cli.sub"
    result = finder.sub(callback, text)
    assert "pkg.commands.sub_module" in result
    assert result.split("import ")[1].strip() == "pkg.commands.sub_module"


def test_replacer_prefix_not_matched_as_word_boundary():
    """pkg.oldx should not be touched by a pkg.old rule."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    assert finder.sub(callback, "pkg.oldx") == "pkg.oldx"
    assert finder.sub(callback, "foo.pkg.old") == "foo.pkg.old"


def test_replacer_file_ext_dotted_not_matched():
    """pkg.old.yaml dotted form should not be matched as a dotted module name."""
    rename_map = {"pkg.old": "pkg.new"}
    finder, callback = build_text_replacer(rename_map, set())
    text = "see pkg.old.yaml for config"
    result = finder.sub(callback, text)
    assert "pkg.old.yaml" in result
