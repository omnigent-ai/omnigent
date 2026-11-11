"""Keep protoc output valid without weakening checks on generated declarations."""

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import gen_protobuf
from scripts.gen_protobuf import normalize_stub


@pytest.mark.parametrize(
    "source,expected",
    [
        ("RESOURCE_FIELD_NUMBER: _ClassVar[int]\n", "RESOURCE_FIELD_NUMBER: int\n"),
        ("    VALUE: _ClassVar[int]\n", "    VALUE: _ClassVar[int]\n"),
        ("VALUE: int\n", "VALUE: int\n"),
    ],
)
def test_normalize_stub(source: str, expected: str) -> None:
    assert normalize_stub(source) == expected
    assert normalize_stub(expected) == expected


def test_generate_message_bindings_for_all_schemas_and_connect_only_for_owned_apis(
    tmp_path: Path,
) -> None:
    protos = gen_protobuf._protos()
    for proto in protos:
        stub = tmp_path / proto.with_name(f"{proto.stem}_pb2.pyi")
        stub.parent.mkdir(parents=True, exist_ok=True)
        stub.write_text("RESOURCE_FIELD_NUMBER: _ClassVar[int]\n")
    with (
        patch.object(gen_protobuf.importlib.util, "find_spec", return_value=object()),
        patch.object(gen_protobuf, "_CONNECT_PLUGIN", tmp_path),
        patch.object(gen_protobuf.subprocess, "run") as run,
    ):
        gen_protobuf._run_protoc(tmp_path)
    assert len(run.call_args_list) == len(protos)
    for call, proto in zip(run.call_args_list, protos, strict=True):
        command = call.args[0]
        assert command[-1] == str(proto)
        assert f"--python_out={tmp_path}" in command
        assert f"--pyi_out={tmp_path}" in command
        assert (f"--connectrpc_out={tmp_path}" in command) == (
            not proto.is_relative_to(Path("omnigent/api/substrate"))
        )
        assert call.kwargs == {"check": True, "cwd": gen_protobuf._REPO_ROOT}
        stub = tmp_path / proto.with_name(f"{proto.stem}_pb2.pyi")
        assert stub.read_text() == "RESOURCE_FIELD_NUMBER: int\n"
