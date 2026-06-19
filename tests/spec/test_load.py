"""Tests for omnigent.spec.load()."""

from __future__ import annotations

import io
import tarfile
import textwrap
from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec import load, materialize_bundle
from omnigent.spec._omnigent_compat import load_omnigent_yaml


@pytest.fixture()
def agent_dir(tmp_path: Path) -> Path:
    """Create a minimal valid agent image directory."""
    config = {
        "spec_version": 1,
        "name": "test-agent",
        "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
    }
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def _make_tarball(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a tar.gz at tmp_path/bundle.tar.gz."""
    tar_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


def test_load_from_directory(agent_dir: Path) -> None:
    spec = load(agent_dir)
    assert spec.name == "test-agent"
    assert spec.spec_version == 1


def test_load_from_tarball(tmp_path: Path) -> None:
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "tarball-agent",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        }
    )
    tar_path = _make_tarball(tmp_path, {"config.yaml": config})
    dest = tmp_path / "extracted"

    spec = load(tar_path, dest=dest)

    assert spec.name == "tarball-agent"
    assert dest.is_dir()
    assert (dest / "config.yaml").exists()


def test_load_tarball_without_dest_raises(tmp_path: Path) -> None:
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "x",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        }
    )
    tar_path = _make_tarball(tmp_path, {"config.yaml": config})

    with pytest.raises(OmnigentError, match="dest is required"):
        load(tar_path)


def test_load_yaml_missing_prompt_raises_actionable_error(tmp_path: Path) -> None:
    """
    A ``.yaml`` file that's missing the ``prompt`` key fails the
    omnigent-YAML check. Without the diagnostic dispatch in
    ``load()``, the caller would see ``"dest is required when
    loading from a tarball"`` — the file's a YAML, not a tarball.
    The fix surfaces the specific reason instead.

    What breaks if this fails: a user editing the prompt field in
    their YAML and accidentally deleting it gets an unhelpful
    "tarball" error and no idea what's wrong.
    """
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(yaml.dump({"name": "x"}))  # missing 'prompt'

    with pytest.raises(OmnigentError) as excinfo:
        load(yaml_path)
    msg = str(excinfo.value)
    # Must NOT show the misleading tarball error.
    assert "tarball" not in msg, (
        f"Error mentions 'tarball' for a YAML file: {msg!r}. "
        f"The diagnostic-dispatch branch should have fired instead."
    )
    # Must name the actual problem. The error wording is
    # "missing system-prompt key" and points to ``prompt:`` /
    # ``instructions:`` as the missing keys.
    assert "missing system-prompt key" in msg
    assert "'prompt:'" in msg


def test_load_yaml_with_spec_version_raises_actionable_error(tmp_path: Path) -> None:
    """
    A ``.yaml`` file with ``spec_version`` set looks like an
    omnigent spec but is sitting alone (no ``config.yaml``
    bundle dir). The omnigent check rejects it. The error
    should explain the bundle-vs-single-file distinction, not
    the misleading tarball message.
    """
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(yaml.dump({"spec_version": 1, "name": "x", "prompt": "hi"}))

    with pytest.raises(OmnigentError) as excinfo:
        load(yaml_path)
    msg = str(excinfo.value)
    assert "tarball" not in msg
    assert "spec_version" in msg


def test_load_yaml_with_parse_error_raises_actionable_error(tmp_path: Path) -> None:
    """
    A ``.yaml`` file that PyYAML can't parse (e.g. an unquoted
    colon in a value) falls through ``is_omnigent_yaml`` and,
    without the diagnostic dispatch, would surface as the bare
    "tarball" error. The fix forwards PyYAML's location info.
    """
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("name: x\nprompt: Tell me: a story\n")  # unquoted colon

    with pytest.raises(OmnigentError) as excinfo:
        load(yaml_path)
    msg = str(excinfo.value)
    assert "tarball" not in msg
    assert "YAML parse error" in msg


def test_load_invalid_spec_raises(tmp_path: Path) -> None:
    config = {"spec_version": 99, "name": "bad"}
    (tmp_path / "config.yaml").write_text(yaml.dump(config))

    with pytest.raises(OmnigentError, match="invalid agent spec"):
        load(tmp_path)


def test_load_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="source not found"):
        load(tmp_path / "nonexistent")


def test_load_from_bytes(tmp_path: Path) -> None:
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "bytes-agent",
            "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
        }
    )
    tar_path = _make_tarball(tmp_path, {"config.yaml": config})
    bundle_bytes = tar_path.read_bytes()
    dest = tmp_path / "extracted"

    spec = load(bundle_bytes, dest=dest)
    assert spec.name == "bytes-agent"
    assert dest.is_dir()


def test_load_bytes_without_dest_raises() -> None:
    with pytest.raises(OmnigentError, match="dest is required"):
        load(b"fake-tarball-bytes")


# ── materialize_bundle ──────────────────────────────────────


def test_materialize_bundle_copies_directory_recursively(tmp_path: Path) -> None:
    """
    A directory source is copied recursively into *dest*. This is
    the path taken by native omnigent agent-image directories
    (``config.yaml`` + bundled assets). Every file at any depth
    must survive the copy so ``spec.load(bundle_dir)`` sees the
    full spec tree.

    What breaks if this fails: ``omnigent server --agent my-agent/``
    ends up tarballing a partial bundle — the server-side
    extraction would either fail or produce an incomplete spec
    the LLM runs with.
    """
    source = tmp_path / "source"
    (source / "sub").mkdir(parents=True)
    (source / "config.yaml").write_text("spec_version: 1\nname: x\n")
    (source / "sub" / "asset.txt").write_text("hello")

    dest = tmp_path / "dest"
    returned = materialize_bundle(source, dest)

    # Return value is *dest* itself — callers chain directly.
    assert returned == dest
    assert dest.is_dir()
    # Top-level and nested files both copied — a shallow copy
    # would fail the nested assertion.
    assert (dest / "config.yaml").read_text() == "spec_version: 1\nname: x\n"
    assert (dest / "sub" / "asset.txt").read_text() == "hello"


def test_materialize_bundle_wraps_yaml_file_in_dest(tmp_path: Path) -> None:
    """
    A single-file YAML source is placed at the root of *dest*
    with its original basename preserved. This is the shape
    ``_find_omnigent_yaml_in_dir`` expects — it scans the
    directory for exactly one YAML not named ``config.yaml``.
    Wrong placement (nested directory, renamed file) would fall
    through the dispatch and the load would fail.
    """
    source = tmp_path / "coding_supervisor.yaml"
    source.write_text("name: coding_supervisor\nprompt: hi\n")

    dest = tmp_path / "bundle"
    returned = materialize_bundle(source, dest)

    assert returned == dest
    assert dest.is_dir()
    # Basename preserved — Omnigent' dispatch uses
    # ``is_omnigent_yaml`` on the exact file (not a synthesized
    # ``config.yaml``), so the original name must carry through.
    assert (dest / "coding_supervisor.yaml").read_text() == source.read_text()


def test_materialize_bundle_creates_dest_for_file_source(tmp_path: Path) -> None:
    """
    The file-source branch creates *dest* when it doesn't exist
    (``mkdir(parents=True, exist_ok=True)``). This matches the
    callers' pattern of passing a fresh ``workdir / "bundle"``
    path that hasn't been created yet.
    """
    source = tmp_path / "foo.yaml"
    source.write_text("name: foo\n")

    # Destination doesn't exist yet — materialize must create it.
    dest = tmp_path / "does" / "not" / "exist" / "bundle"
    assert not dest.exists()

    materialize_bundle(source, dest)

    assert dest.is_dir()
    assert (dest / "foo.yaml").exists()


def test_materialize_bundle_missing_source_raises(tmp_path: Path) -> None:
    """
    Non-existent *source* raises :class:`FileNotFoundError` with a
    message pointing at the missing path. Callers should not have
    to probe existence ahead of time; the error is the contract.
    """
    missing = tmp_path / "ghost.yaml"
    with pytest.raises(FileNotFoundError, match="source not found"):
        materialize_bundle(missing, tmp_path / "bundle")


def test_materialize_bundle_then_load_roundtrip_directory(tmp_path: Path) -> None:
    """
    End-to-end sanity: materializing a directory and then calling
    :func:`load` on the materialized path returns the same spec
    the caller would have gotten from ``load(source)`` directly.
    Proves the helper's output is the canonical input to
    :func:`load` — which is the contract all three consumers
    (``_preregister_agent``, ``_prepare_omnigent_yaml_bundle``,
    ``_chat_local``) rely on.
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.yaml").write_text(
        yaml.dump(
            {
                "spec_version": 1,
                "name": "roundtrip-agent",
                "executor": {"type": "omnigent", "config": {"harness": "claude-sdk"}},
            }
        )
    )

    bundle_dir = materialize_bundle(source, tmp_path / "bundle")
    spec = load(bundle_dir)

    assert spec.name == "roundtrip-agent"


def test_materialize_bundle_then_load_roundtrip_yaml(tmp_path: Path) -> None:
    """
    Same end-to-end sanity for the YAML-file branch: materializing
    a standalone omnigent YAML must produce a directory
    :func:`load` can dispatch through ``_find_omnigent_yaml_in_dir``.
    Gate against regressions that would make the wrap-a-file path
    yield a directory ``load`` rejects.
    """
    source = tmp_path / "hello.yaml"
    # Include an executor block with a harness — the adapter's
    # validator requires one for executor.type='omnigent'. The
    # roundtrip shape we want to prove is "path goes through, spec
    # loads" — not harness-selection logic, which has its own
    # tests in test_omnigent_adapter.py.
    source.write_text(
        yaml.dump(
            {
                "name": "hello-from-yaml",
                "prompt": "hi",
                "executor": {
                    "model": "databricks-claude-sonnet-4",
                    "harness": "claude-sdk",
                },
            },
        ),
    )

    bundle_dir = materialize_bundle(source, tmp_path / "bundle")
    spec = load(bundle_dir)

    # Omnigent-sourced spec — translator sets executor.type.
    assert spec.name == "hello-from-yaml"
    assert spec.executor.type == "omnigent"


def test_load_omnigent_yaml_preserves_use_responses_bool(tmp_path: Path) -> None:
    """
    ``use_responses: false`` in an omnigent YAML must land as Python ``False``
    on ``spec.executor.config["use_responses"]``, not the string ``"false"``.

    What breaks if this fails: ``_build_openai_agents_sdk_spawn_env`` encodes the
    config value with ``bool(use_responses)`` — but ``bool("false") is True``, so a
    string ``"false"`` inverts the flag and the executor is launched with
    ``HARNESS_OPENAI_AGENTS_USE_RESPONSES=true``, causing silent API failures for
    models that require ``use_responses=False`` (e.g. Kimi K2 via Databricks).

    Root cause guarded: ``_omnigent_compat.load_omnigent_yaml`` previously used
    ``yaml.safe_load`` to read the raw YAML, but importing ``load_agent_def`` mutates
    ``yaml.SafeLoader``'s implicit resolvers as a side effect, causing ``safe_load`` to
    return string ``"false"`` for unquoted ``false`` values.  The fix is to use
    ``_OmnigentYamlLoader`` directly (which owns its resolvers and is unaffected by
    the mutation).
    """
    yaml_text = textwrap.dedent("""\
        name: kimi-test
        prompt: You are a helpful assistant.

        executor:
          harness: openai-agents
          model: databricks-kimi-k2-6
          use_responses: false
    """)
    (tmp_path / "kimi-test.yaml").write_text(yaml_text)
    spec = load_omnigent_yaml(tmp_path / "kimi-test.yaml")
    assert spec.executor.config.get("use_responses") is False


def test_load_omnigent_yaml_threads_executor_extra_max_tokens_to_llm_extra(
    tmp_path: Path,
) -> None:
    """
    Omnigent-compatible YAML carries generation kwargs under
    ``executor.extra``; the Omnigent compatibility loader must translate
    those into ``spec.llm.extra`` so harness-backed Omnigent execution can
    forward them to the inner LLM executor.
    """
    yaml_text = textwrap.dedent("""\
        name: kimi-test
        prompt: You are a helpful assistant.

        executor:
          harness: openai-agents
          model: databricks-kimi-k2-6
          use_responses: false
          extra:
            max_tokens: 65536
    """)
    (tmp_path / "kimi-test.yaml").write_text(yaml_text)

    spec = load_omnigent_yaml(tmp_path / "kimi-test.yaml")

    assert spec.llm is not None
    assert spec.llm.extra["max_tokens"] == 65536


def test_load_omnigent_yaml_unknown_harness_hints_at_version_skew(
    tmp_path: Path,
) -> None:
    """
    An unrecognized harness is most often a client-older-than-server skew:
    the server emitted a harness this runner's allowlist doesn't know yet.
    The synthesized-spec error must append a disclaimer pointing the operator
    at that version skew rather than implying the spec itself is malformed.
    """
    yaml_text = textwrap.dedent("""\
        name: skew-test
        prompt: You are a helpful assistant.

        executor:
          harness: totally-made-up-harness
          model: databricks-claude-sonnet-4
    """)
    (tmp_path / "skew-test.yaml").write_text(yaml_text)

    with pytest.raises(OmnigentError) as excinfo:
        load_omnigent_yaml(tmp_path / "skew-test.yaml")

    message = str(excinfo.value)
    assert "executor.config.harness" in message
    assert "this client (runner) may be older than the server" in message


def test_load_omnigent_yaml_missing_harness_omits_version_skew_hint(
    tmp_path: Path,
) -> None:
    """
    The version-skew hint is gated on a harness *enum mismatch*, not on any
    ``executor.config.harness`` failure. A *missing* harness is a plain
    authoring mistake — the synthesized-spec error still fires (same path),
    but it must NOT imply the client is older than the server, since no
    unrecognized harness value is involved.
    """
    yaml_text = textwrap.dedent("""\
        name: no-harness-test
        prompt: You are a helpful assistant.
    """)
    (tmp_path / "no-harness-test.yaml").write_text(yaml_text)

    with pytest.raises(OmnigentError) as excinfo:
        load_omnigent_yaml(tmp_path / "no-harness-test.yaml")

    message = str(excinfo.value)
    # Assert the exact "required when ..." variant (not just the field path):
    # this keeps the test from passing vacuously if the default executor type
    # ever stops routing a harness-less spec through the missing-harness branch.
    assert "executor.config.harness" in message
    assert "required when" in message
    assert "this client (runner) may be older than the server" not in message
