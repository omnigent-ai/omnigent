"""One-shot framework context owned by a native Claude prompt and terminal launch."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from omnigent.harnesses.claude_native.submit_verification import (
    SubmitBoundary,
    has_matching_local_command_completion,
    parse_submit_boundary,
    serialize_submit_boundary,
)
from omnigent.inner.terminal_lifecycle import TERMINAL_INSTANCE_ID_ENV, TERMINAL_LAUNCH_ID_ENV
from omnigent.native.native_bridge_common import bridge_dir_preparation_lock

if TYPE_CHECKING:
    from omnigent.harnesses.claude_native.bridge import ClaudeDeliveryOwner

PROMPT_FRAMEWORK_CONTEXT_DIR = "pending_framework_context"
PROMPT_FRAMEWORK_CONTEXT_CAPABILITY_PREFIX = "prompt_framework_context_capability-"
MAX_PENDING_PROMPT_CONTEXTS = 128


class PromptFrameworkContextPending(RuntimeError):
    """An earlier indistinguishable prompt still owns the next matching hook."""


@dataclass(frozen=True)
class PromptFrameworkContextHandle:
    """Identify exactly the context entry one injection may discard."""

    path: Path
    owner: ClaudeDeliveryOwner
    record_id: str


def _owner_payload(owner: ClaudeDeliveryOwner) -> dict[str, object]:
    # JSON normalizes the legacy advertisement tuple for persisted comparison.
    return json.loads(json.dumps(asdict(owner)))


def _prompt_digest(prompt: str) -> str:
    from omnigent.harnesses.claude_native.bridge import _unwrap_pasted_content_markers

    normalized = _unwrap_pasted_content_markers(prompt).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.strip().encode("utf-8")).hexdigest()


def _capability_path(bridge_dir: Path, launch_id: object) -> Path | None:
    if not isinstance(launch_id, str) or not re.fullmatch(r"[0-9a-f]{32}", launch_id):
        return None
    return bridge_dir / f"{PROMPT_FRAMEWORK_CONTEXT_CAPABILITY_PREFIX}{launch_id}.json"


def record_prompt_framework_context_capability(
    bridge_dir: Path,
    payload: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Attest new SessionStart hook adoption using the actual hook process identities."""
    from omnigent.harnesses.claude_native.bridge import (
        _write_json_file,
        is_root_claude_hook_payload,
        read_delivery_owner,
    )

    if payload.get("hook_event_name") != "SessionStart" or not is_root_claude_hook_payload(
        payload
    ):
        return False
    source_env = os.environ if env is None else env
    launch_id = source_env.get(TERMINAL_LAUNCH_ID_ENV)
    instance_id = source_env.get(TERMINAL_INSTANCE_ID_ENV)
    path = _capability_path(bridge_dir, launch_id)
    if (
        path is None
        or not isinstance(instance_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", instance_id)
    ):
        return False
    with bridge_dir_preparation_lock(bridge_dir):
        owner = read_delivery_owner(bridge_dir)
        if (
            not owner.session_id
            or not owner.native_session_id
            or payload.get("session_id") != owner.native_session_id
            or (
                owner.launch_id == launch_id
                and owner.instance_id is not None
                and owner.instance_id != instance_id
            )
        ):
            return False
        # SessionStart can precede the advertisement; readiness checks it later.
        _write_json_file(
            path,
            {
                "schema_version": 1,
                "session_id": owner.session_id,
                "native_session_id": owner.native_session_id,
                "launch_id": launch_id,
                "instance_id": instance_id,
            },
        )
    return True


def _capability_matches(bridge_dir: Path, owner: ClaudeDeliveryOwner) -> bool:
    from omnigent.harnesses.claude_native.bridge import _read_json_file

    path = _capability_path(bridge_dir, owner.launch_id)
    if path is None or not owner.instance_id:
        return False
    try:
        record = _read_json_file(path)
    except OSError:
        return False
    return (
        type(record.get("schema_version")) is int
        and record["schema_version"] == 1
        and set(record)
        == {"schema_version", "session_id", "native_session_id", "launch_id", "instance_id"}
        and isinstance(record.get("session_id"), str)
        and bool(record["session_id"])
        and isinstance(record.get("native_session_id"), str)
        and bool(record["native_session_id"])
        and record.get("launch_id") == owner.launch_id
        and record.get("instance_id") == owner.instance_id
    )


def prompt_framework_context_capability_ready(bridge_dir: Path) -> bool:
    """Require hook adoption by this physical launch, independent of session remapping."""
    from omnigent.harnesses.claude_native.bridge import read_delivery_owner

    with bridge_dir_preparation_lock(bridge_dir):
        return _capability_matches(bridge_dir, read_delivery_owner(bridge_dir))


def _remove_empty_spool(spool: Path) -> None:
    with contextlib.suppress(OSError):
        spool.rmdir()


def _valid_owner(value: object, expected: dict[str, object]) -> bool:
    if not isinstance(value, dict) or value.keys() != expected.keys():
        return False
    for key, component in value.items():
        if key == "legacy_advertisement":
            if component is not None and not (
                isinstance(component, list)
                and len(component) == 3
                and all(type(part) is int for part in component)
            ):
                return False
        elif key in {"session_id", "native_session_id"}:
            if not isinstance(component, str) or not component:
                return False
        elif component is not None and not isinstance(component, str):
            return False
    return True


def _pending_records(
    spool: Path, owner: dict[str, object]
) -> list[tuple[Path, dict[str, object]]]:
    from omnigent.harnesses.claude_native.bridge import _read_json_file

    records: list[tuple[Path, dict[str, object]]] = []
    for path in spool.glob("*.json"):
        try:
            record = _read_json_file(path)
            created_at = record.get("created_at")
            recorded_owner = record.get("owner")
            record_id = record.get("record_id")
            prompt_digest = record.get("prompt_sha256")
            valid = (
                record.get("schema_version") == 1
                and isinstance(record_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", record_id)
                and record_id == path.stem
                and _valid_owner(recorded_owner, owner)
                and isinstance(prompt_digest, str)
                and re.fullmatch(r"[0-9a-f]{64}", prompt_digest)
                and isinstance(record.get("context"), str)
                and isinstance(created_at, (int, float))
                and not isinstance(created_at, bool)
                and math.isfinite(created_at)
                and (
                    record.get("submit_boundary") is None
                    or parse_submit_boundary(record["submit_boundary"]) is not None
                )
            )
            if not valid:
                raise PromptFrameworkContextPending(
                    "Claude Code's previous input ownership could not be verified."
                )
            if record["owner"] == owner:
                records.append((path, record))
            else:
                path.unlink(missing_ok=True)
        except (OSError, OverflowError, ValueError) as exc:
            raise PromptFrameworkContextPending(
                "Claude Code's previous input ownership could not be read yet."
            ) from exc
    return records


def write_prompt_framework_context(
    bridge_dir: Path,
    prompt: str,
    notices: Sequence[str],
    *,
    submit_boundary: SubmitBoundary | None = None,
    expected_owner: ClaudeDeliveryOwner | None = None,
) -> PromptFrameworkContextHandle:
    """Stage context without replacing an earlier prompt whose delivery is uncertain."""
    from omnigent.harnesses.claude_native.bridge import (
        ClaudeMessageNotAccepted,
        _current_delivery_owner,
        _write_json_file,
        read_delivery_owner,
        read_transcript_path,
    )

    context = "\n\n".join(notice for notice in notices if notice)
    spool = bridge_dir / PROMPT_FRAMEWORK_CONTEXT_DIR
    with bridge_dir_preparation_lock(bridge_dir):
        owner = (
            _current_delivery_owner(bridge_dir, expected_owner)
            if expected_owner is not None
            else read_delivery_owner(bridge_dir)
        )
        if owner is None:
            raise ClaudeMessageNotAccepted("Claude's input owner changed; nothing was sent.")
        if not owner.session_id or not owner.native_session_id:
            raise ClaudeMessageNotAccepted(
                "Claude Code's session identity is not ready for this message's framework context."
            )
        if context and not owner.launch_id:
            raise ClaudeMessageNotAccepted(
                "Claude Code's terminal launch identity is not ready for framework context."
            )
        if not _capability_matches(bridge_dir, owner):
            raise ClaudeMessageNotAccepted(
                "This Claude terminal has not loaded the current web-input hooks. "
                "Relaunch or resume the terminal, then retry this message."
            )
        owner_payload = _owner_payload(owner)
        now = time.time()
        records = _pending_records(spool, owner_payload)
        prompt_digest = _prompt_digest(prompt)
        transcript = read_transcript_path(bridge_dir)
        current_path = transcript.resolve() if transcript is not None else None
        if submit_boundary is not None and (
            submit_boundary.native_session_id != owner.native_session_id
            or submit_boundary.path != current_path
        ):
            raise ClaudeMessageNotAccepted(
                "Claude Code's native transcript changed before this message was sent."
            )
        retained: list[tuple[Path, dict[str, object]]] = []
        for path, record in records:
            checkpoint = parse_submit_boundary(record.get("submit_boundary"))
            if (
                record["prompt_sha256"] == prompt_digest
                and record["context"] == ""
                and checkpoint is not None
                and checkpoint.native_session_id == owner.native_session_id
                and checkpoint.path == current_path
                and has_matching_local_command_completion(checkpoint, prompt)
            ):
                path.unlink(missing_ok=True)
            else:
                retained.append((path, record))
        records = retained
        if any(record["prompt_sha256"] == prompt_digest for _, record in records):
            raise PromptFrameworkContextPending(
                "An earlier identical Claude input is still awaiting its submission hook."
            )
        if len(records) >= MAX_PENDING_PROMPT_CONTEXTS:
            raise ClaudeMessageNotAccepted(
                "Claude Code has too many pending framework contexts. Wait before sending again."
            )
        record_id = uuid.uuid4().hex
        path = spool / f"{record_id}.json"
        # Plain inputs also fence a later identical prompt's different context.
        _write_json_file(
            path,
            {
                "schema_version": 1,
                "record_id": record_id,
                "owner": owner_payload,
                "prompt_sha256": prompt_digest,
                "created_at": now,
                "context": context,
                "submit_boundary": (
                    serialize_submit_boundary(submit_boundary)
                    if submit_boundary is not None
                    else None
                ),
            },
        )
        return PromptFrameworkContextHandle(path=path, owner=owner, record_id=record_id)


def discard_prompt_framework_context(
    handle: PromptFrameworkContextHandle | None, *, retain_fence: bool = False
) -> None:
    """Discard one notice; keep a fence when its native hook can still arrive."""
    if handle is None:
        return
    from omnigent.harnesses.claude_native.bridge import _read_json_file, _write_json_file

    bridge_dir = handle.path.parent.parent
    with bridge_dir_preparation_lock(bridge_dir):
        record = _read_json_file(handle.path)
        if record.get("record_id") == handle.record_id and record.get("owner") == _owner_payload(
            handle.owner
        ):
            if retain_fence:
                record["context"] = ""
                _write_json_file(handle.path, record)
            else:
                handle.path.unlink(missing_ok=True)
        _remove_empty_spool(handle.path.parent)


def consume_prompt_framework_context(
    bridge_dir: Path, payload: Mapping[str, object]
) -> str | None:
    """Consume a matching prompt once; leave ambiguous or foreign prompts untouched."""
    from omnigent.harnesses.claude_native.bridge import (
        is_root_claude_hook_payload,
        read_delivery_owner,
    )

    with bridge_dir_preparation_lock(bridge_dir):
        spool = bridge_dir / PROMPT_FRAMEWORK_CONTEXT_DIR
        if not spool.exists():
            return None
        owner = read_delivery_owner(bridge_dir)
        prompt = payload.get("prompt")
        if (
            payload.get("hook_event_name") != "UserPromptSubmit"
            or not is_root_claude_hook_payload(payload)
            or not isinstance(prompt, str)
            or not owner.session_id
            or not owner.native_session_id
            or payload.get("session_id") != owner.native_session_id
            or (
                owner.launch_id is not None
                and os.environ.get(TERMINAL_LAUNCH_ID_ENV) != owner.launch_id
            )
        ):
            return None
        records = _pending_records(spool, _owner_payload(owner))
        matches = [
            (path, record)
            for path, record in records
            if record["prompt_sha256"] == _prompt_digest(prompt)
        ]
        if not matches or len({record["context"] for _, record in matches}) != 1:
            _remove_empty_spool(spool)
            return None
        path, record = min(
            matches, key=lambda item: (cast(float, item[1]["created_at"]), item[0].name)
        )
        if record["context"] and not owner.launch_id:
            return None
        path.unlink(missing_ok=True)
        _remove_empty_spool(spool)
        context = record["context"]
        return context if isinstance(context, str) else None
