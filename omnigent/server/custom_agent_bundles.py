"""Scalar edits retain every unrelated member of an uploaded Agent archive."""

from __future__ import annotations

import copy
import io
import json
import re
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec import extract_safe

MAX_BUNDLE_BYTES = 32 * 1024 * 1024
_MANAGED_INSTRUCTIONS_PATH = re.compile(r"catalog-instructions-[0-9a-f]{32}\.md")


def _managed_instructions_path(raw: str) -> str | None:
    """Return our existing generated instructions member, when present."""
    node = yaml.compose(raw)
    if not isinstance(node, yaml.MappingNode):
        return None
    for key, value in node.value:
        if (
            key.value == "instructions"
            and isinstance(value, yaml.ScalarNode)
            and _MANAGED_INSTRUCTIONS_PATH.fullmatch(value.value)
        ):
            return value.value
    return None


def _patch_yaml_fields(raw: str, fields: dict[str, Any]) -> str:
    """Edit scalar values while preserving unrelated YAML and alias values."""
    node = yaml.compose(raw)
    if not isinstance(node, yaml.MappingNode):
        raise OmnigentError("Agent configuration must be a mapping", code=ErrorCode.INVALID_INPUT)
    tokens = list(yaml.scan(raw))
    edits: list[tuple[int, int, str]] = []
    found: set[str] = set()
    detached_anchors: dict[str, str] = {}
    for index, (key, value) in enumerate(node.value):
        if key.value not in fields:
            continue
        if key.value in found:
            raise OmnigentError(
                "Duplicate editable configuration key", code=ErrorCode.INVALID_INPUT
            )
        found.add(key.value)
        end = (
            node.value[index + 1][0].start_mark.index
            if index + 1 < len(node.value)
            else node.end_mark.index
        )
        alias = next(
            (
                token
                for token in tokens
                if isinstance(token, yaml.AliasToken)
                and key.end_mark.index <= token.start_mark.index < end
            ),
            None,
        )
        if alias is not None:
            start, stop = alias.start_mark.index, alias.end_mark.index
        else:
            start, stop = value.start_mark.index, value.end_mark.index
            for token in tokens:
                if isinstance(token, yaml.AnchorToken) and start <= token.start_mark.index < stop:
                    if not isinstance(value, yaml.ScalarNode):
                        raise OmnigentError(
                            "Editable fields must be scalars", code=ErrorCode.INVALID_INPUT
                        )
                    original = copy.copy(value)
                    original.style = '"'
                    detached_anchors[token.value] = yaml.serialize(original).strip()
        replacement = json.dumps(fields[key.value], ensure_ascii=False)
        if raw[start:stop].endswith("\n"):
            replacement += "\n"
        edits.append((start, stop, replacement))
    # Detach references to replaced anchors so other fields retain their values.
    for token in tokens:
        if isinstance(token, yaml.AliasToken) and token.value in detached_anchors:
            if not any(start <= token.start_mark.index < stop for start, stop, _ in edits):
                edits.append(
                    (token.start_mark.index, token.end_mark.index, detached_anchors[token.value])
                )
    missing = fields.keys() - found
    if missing:
        additions = [
            f"{json.dumps(key)}: {json.dumps(fields[key], ensure_ascii=False)}"
            for key in sorted(missing)
        ]
        if node.flow_style:
            close = node.end_mark.index - 1
            before_close = [token for token in tokens if token.end_mark.index <= close]
            trailing_comma = bool(before_close) and isinstance(
                before_close[-1], yaml.FlowEntryToken
            )
            separator = ", " if node.value and not trailing_comma else " "
            edits.append((close, close, separator + ", ".join(additions)))
        else:
            end = node.end_mark.index
            separator = "" if raw[:end].endswith("\n") else "\n"
            edits.append((end, end, separator + "\n".join(additions) + "\n"))
    for start, stop, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[stop:]
    return raw


def patch_bundle(bundle: bytes, changes: dict[str, Any]) -> bytes:
    """Patch top-level YAML scalars without reserializing unknown configuration."""
    with tempfile.TemporaryDirectory() as temp:
        root = extract_safe(bundle, Path(temp) / "bundle")
        config = root / "config.yaml"
        if not config.is_file():
            candidates = [*root.glob("*.yaml"), *root.glob("*.yml")]
            if len(candidates) != 1:
                raise OmnigentError(
                    "Agent bundle has no unambiguous root configuration",
                    code=ErrorCode.INVALID_INPUT,
                )
            config = candidates[0]
        raw = config.read_text(encoding="utf-8")
        replacements: dict[str, bytes] = {}
        fields = dict(changes)
        if "instructions" in fields:
            # Use a generated file so inline text matching a bundle filename is
            # never loaded as that file. Reuse our prior path to avoid growing
            # the archive on every edit.
            path = _managed_instructions_path(raw) or f"catalog-instructions-{uuid.uuid4().hex}.md"
            replacements[path] = (fields["instructions"] or "").encode("utf-8")
            fields["instructions"] = path
        replacements[config.name] = _patch_yaml_fields(raw, fields).encode("utf-8")

    output = io.BytesIO()
    seen: set[str] = set()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:*") as source,
        tarfile.open(fileobj=output, mode="w:gz") as target,
    ):
        for member in source:
            normalized = str(PurePosixPath(member.name))
            if normalized in seen:
                raise OmnigentError(
                    "Duplicate archive member; upload a normalized bundle to edit",
                    code=ErrorCode.INVALID_INPUT,
                )
            seen.add(normalized)
            if normalized in replacements and member.isfile():
                data = replacements.pop(normalized)
                info = copy.copy(member)
                info.pax_headers = dict(info.pax_headers)
                info.pax_headers.pop("size", None)
                info.size = len(data)
                target.addfile(info, io.BytesIO(data))
            else:
                target.addfile(member, source.extractfile(member) if member.isfile() else None)
        for path, data in replacements.items():
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o600
            target.addfile(info, io.BytesIO(data))
    return output.getvalue()
