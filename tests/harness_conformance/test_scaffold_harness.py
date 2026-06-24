"""Tests for the harness scaffold generator."""

from __future__ import annotations

from omnigent.scaffold_harness import (
    EXTENSION_CHECKLIST,
    _class_name,
    _module_name,
    generate_harness_scaffold,
    main,
)


def test_name_helpers() -> None:
    assert _module_name("foo-native") == "foo_native"
    assert _class_name("foo-native") == "FooNative"


def test_generate_scaffold_files() -> None:
    files = generate_harness_scaffold("foo-native", family="native-server", transport="http-sse")
    assert "omnigent/inner/foo_native_harness.py" in files
    assert "omnigent/inner/foo_native_executor.py" in files
    assert "tests/test_foo_native_harness.py" in files
    harness = files["omnigent/inner/foo_native_harness.py"]
    assert "def create_app() -> FastAPI:" in harness
    assert "FooNativeExecutor" in harness
    executor = files["omnigent/inner/foo_native_executor.py"]
    assert "class FooNativeExecutor(Executor):" in executor
    # descriptor stub present (as a comment key).
    stub_key = next(k for k in files if k.startswith("#"))
    assert 'id="foo-native"' in files[stub_key]


def test_generated_harness_module_is_syntactically_valid() -> None:
    files = generate_harness_scaffold("foo-native")
    for path, content in files.items():
        if path.endswith(".py"):
            compile(content, path, "exec")


def test_main_dry_run(capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["foo-native"])
    assert code == 0
    out = capsys.readouterr().out
    assert "create_app" in out
    assert EXTENSION_CHECKLIST[0] in out


def test_main_write(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    code = main(["foo-native", "--write", "--root", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "omnigent/inner/foo_native_harness.py").is_file()
    assert (tmp_path / "omnigent/inner/foo_native_executor.py").is_file()
    assert (tmp_path / "tests/test_foo_native_harness.py").is_file()
