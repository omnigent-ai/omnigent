"""Opt-in live canary machinery for native compaction followed by cold resume.

This module deliberately drives real Claude Code/Codex TUIs. It is imported by
the two provider-specific test modules; it is not collected as a test itself.
The canary requires an explicit provider opt-in and a real authenticated
``$HOME``. It never sends signals by process name: only fixture-owned PTY PIDs
and the session deletion API are used during teardown.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from omnigent.codex_native_bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    codex_home_for_bridge_dir,
)
from omnigent.codex_native_bridge import (
    bridge_dir_for_bridge_id as codex_bridge_dir,
)
from omnigent.entities.session_resources import terminal_resource_id
from tests.e2e._native_resume_helpers import (
    PtyHandle,
    cli_env,
    inject_user_message,
    omnigent_console_script,
    poll_external_session_id,
    spawn_cli_background,
    wait_for_conversation_id,
    wait_for_terminal_ready,
)
from tests.e2e.helpers import POLL_INTERVAL_S

Harness = Literal["claude", "codex"]
ResumeMode = Literal["local-artifact", "server-reconstruction"]

_COMPACTION_WAIT_S = 90.0
_TURN_WAIT_S = 180.0
_START_WAIT_S = 120.0
_EXIT_WAIT_S = 45.0


def configured_session_count(env_var: str) -> int:
    """Return the requested session count, enforcing the canary minimum."""
    raw = os.environ.get(env_var, "3")
    try:
        count = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_var} must be an integer, got {raw!r}") from exc
    if count < 3:
        raise ValueError(f"{env_var} must be >= 3, got {count}")
    return count


def canary_cases(env_var: str) -> list[tuple[int, ResumeMode]]:
    """Build deterministic cases that cover both resume algorithms."""
    return [
        (index, "server-reconstruction" if index % 2 else "local-artifact")
        for index in range(configured_session_count(env_var))
    ]


def _json_lines(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"invalid JSONL in native artifact {path}:{line_number}: {exc}"
                ) from exc
            if isinstance(value, dict):
                records.append(value)
    return records


def _marker_in(value: object, marker: str) -> bool:
    return marker in json.dumps(value, sort_keys=True, ensure_ascii=False)


def claude_compact_record(path: Path, marker: str) -> dict[str, Any] | None:
    """Return Claude's latest marker-retaining ``isCompactSummary`` record."""
    for record in reversed(_json_lines(path)):
        if record.get("isCompactSummary") is True and _marker_in(record, marker):
            return record
    return None


def codex_compact_record(path: Path, marker: str) -> dict[str, Any] | None:
    """Return Codex's latest marker-retaining native ``compacted`` record."""
    for record in reversed(_json_lines(path)):
        payload = record.get("payload")
        if (
            record.get("type") == "compacted"
            and isinstance(payload, dict)
            and isinstance(payload.get("replacement_history"), list)
            and _marker_in(payload["replacement_history"], marker)
        ):
            return record
    return None


def _server_items(client: httpx.Client, conversation_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{conversation_id}/items",
        params={"limit": 500, "order": "asc"},
    )
    response.raise_for_status()
    return [item for item in response.json().get("data", []) if isinstance(item, dict)]


def _assistant_ids(client: httpx.Client, conversation_id: str) -> set[str]:
    return {
        str(item["id"])
        for item in _server_items(client, conversation_id)
        if item.get("role") == "assistant" and isinstance(item.get("id"), str)
    }


def _assistant_text(item: dict[str, Any]) -> str:
    if item.get("role") != "assistant" or not isinstance(item.get("content"), list):
        return ""
    return " ".join(
        str(block["text"])
        for block in item["content"]
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _wait_new_assistant_marker(
    client: httpx.Client,
    *,
    conversation_id: str,
    marker: str,
    previous_ids: set[str],
    timeout: float = _TURN_WAIT_S,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_texts: list[str] = []
    while time.monotonic() < deadline:
        items = _server_items(client, conversation_id)
        last_texts = []
        for item in items:
            text = _assistant_text(item)
            if text:
                last_texts.append(text)
            if item.get("id") not in previous_ids and marker in text:
                return item
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no NEW assistant response containing {marker!r} for {conversation_id}; "
        f"recent assistant texts: {last_texts[-3:]!r}"
    )


def _session_payload(client: httpx.Client, conversation_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{conversation_id}")
    response.raise_for_status()
    payload = response.json()
    assert isinstance(payload, dict)
    return payload


def native_artifacts(
    client: httpx.Client,
    *,
    harness: Harness,
    conversation_id: str,
    external_session_id: str,
) -> list[Path]:
    """Locate only artifacts whose filename contains this test's native id."""
    if harness == "claude":
        root = Path.home() / ".claude" / "projects"
        return sorted(root.glob(f"**/{external_session_id}.jsonl")) if root.is_dir() else []

    payload = _session_payload(client, conversation_id)
    labels = payload.get("labels")
    bridge_id = (
        labels.get(CODEX_NATIVE_BRIDGE_ID_LABEL_KEY) if isinstance(labels, dict) else None
    ) or conversation_id
    home = codex_home_for_bridge_dir(codex_bridge_dir(str(bridge_id)))
    return sorted(home.glob(f"sessions/**/rollout-*-{external_session_id}.jsonl"))


def _wait_native_artifacts(
    client: httpx.Client,
    *,
    harness: Harness,
    conversation_id: str,
    external_session_id: str,
    timeout: float = _START_WAIT_S,
) -> list[Path]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        paths = native_artifacts(
            client,
            harness=harness,
            conversation_id=conversation_id,
            external_session_id=external_session_id,
        )
        if paths:
            return paths
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"no {harness} native artifact for conversation={conversation_id} "
        f"external_session_id={external_session_id}"
    )


def _native_compaction(
    harness: Harness, paths: list[Path], marker: str
) -> tuple[Path, dict[str, Any]] | None:
    parser = claude_compact_record if harness == "claude" else codex_compact_record
    for path in paths:
        record = parser(path, marker)
        if record is not None:
            return path, record
    return None


def _server_compaction(
    client: httpx.Client, conversation_id: str, marker: str
) -> dict[str, Any] | None:
    for item in reversed(_server_items(client, conversation_id)):
        messages = item.get("compacted_messages")
        if item.get("type") == "compaction" and isinstance(messages, list):
            if _marker_in(messages, marker):
                return item
    return None


def _wait_authoritative_compaction(
    client: httpx.Client,
    *,
    harness: Harness,
    conversation_id: str,
    external_session_id: str,
    marker: str,
    timeout: float,
) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        paths = native_artifacts(
            client,
            harness=harness,
            conversation_id=conversation_id,
            external_session_id=external_session_id,
        )
        native = _native_compaction(harness, paths, marker)
        server = _server_compaction(client, conversation_id, marker)
        if native is not None and server is not None:
            path, record = native
            return path, record, server
        time.sleep(POLL_INTERVAL_S)
    return None


def _request_native_compaction(client: httpx.Client, conversation_id: str) -> None:
    response = client.post(
        f"/v1/sessions/{conversation_id}/events",
        json={"type": "compact", "data": {}},
        timeout=30.0,
    )
    assert response.status_code == 202, (
        f"native /compact dispatch failed for {conversation_id}: "
        f"{response.status_code} {response.text}"
    )


def _compact_with_bounded_fill(
    client: httpx.Client,
    *,
    harness: Harness,
    conversation_id: str,
    external_session_id: str,
    marker: str,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    _request_native_compaction(client, conversation_id)
    result = _wait_authoritative_compaction(
        client,
        harness=harness,
        conversation_id=conversation_id,
        external_session_id=external_session_id,
        marker=marker,
        timeout=_COMPACTION_WAIT_S,
    )
    if result is not None:
        return result

    fill_turns = max(0, min(int(os.environ.get("OMNIGENT_NATIVE_CANARY_FILL_TURNS", "2")), 6))
    fill_chars = max(
        1000, min(int(os.environ.get("OMNIGENT_NATIVE_CANARY_FILL_CHARS", "6000")), 20000)
    )
    for fill_index in range(fill_turns):
        ack = f"FILL-ACK-{fill_index}-{marker}"
        before = _assistant_ids(client, conversation_id)
        inject_user_message(
            client,
            conversation_id=conversation_id,
            text=(
                f"Keep remembering {marker}. Read this bounded context filler and reply "
                f"with ONLY {ack}: " + ("context-fill " * (fill_chars // 13))
            ),
        )
        _wait_new_assistant_marker(
            client,
            conversation_id=conversation_id,
            marker=ack,
            previous_ids=before,
        )
        _request_native_compaction(client, conversation_id)
        result = _wait_authoritative_compaction(
            client,
            harness=harness,
            conversation_id=conversation_id,
            external_session_id=external_session_id,
            marker=marker,
            timeout=_COMPACTION_WAIT_S,
        )
        if result is not None:
            return result

    raise AssertionError(
        f"{harness} did not produce BOTH a native compact record and a marker-retaining "
        f"server compaction item for {conversation_id}. Direct /compact plus "
        f"{fill_turns} bounded fill turns failed; increase "
        "OMNIGENT_NATIVE_CANARY_FILL_TURNS/CHARS only if the installed CLI "
        "requires more context pressure."
    )


def _assert_compaction_mirror(
    harness: Harness,
    *,
    marker: str,
    native_record: dict[str, Any],
    server_item: dict[str, Any],
) -> None:
    server_messages = server_item.get("compacted_messages")
    assert isinstance(server_messages, list) and _marker_in(server_messages, marker)
    if harness == "claude":
        # Integration branches that expose the producer source must prove the
        # durable transcript won over the hook fallback race.
        if "snapshot_source" in server_item:
            assert server_item["snapshot_source"] == "transcript", server_item
        assert native_record.get("isCompactSummary") is True
        return

    payload = native_record.get("payload")
    assert isinstance(payload, dict)
    replacement = payload.get("replacement_history")
    assert isinstance(replacement, list)
    assert server_messages == replacement, (
        "Codex rollout replacement_history was not mirrored verbatim server-side"
    )


def _wait_terminal_gone(
    client: httpx.Client, *, conversation_id: str, harness: Harness, timeout: float
) -> None:
    expected = terminal_resource_id(harness, "main")
    deadline = time.monotonic() + timeout
    last_ids: list[str] = []
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{conversation_id}/resources")
        if response.status_code == 200:
            resources = response.json().get("data", [])
            last_ids = [
                str(resource.get("id")) for resource in resources if isinstance(resource, dict)
            ]
            if expected not in last_ids:
                return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"terminal {expected!r} still registered for {conversation_id}; "
        f"resume would be warm, not cold (resources={last_ids!r})"
    )


def _clean_exit(
    handle: PtyHandle,
    client: httpx.Client,
    *,
    conversation_id: str,
    harness: Harness,
) -> None:
    handle.send_line("/exit")
    assert handle.wait(_EXIT_WAIT_S), (
        f"{harness} did not exit cleanly after /exit for {conversation_id}; "
        f"PTY tail:\n{handle.output()[-3000:]}"
    )
    _wait_terminal_gone(
        client,
        conversation_id=conversation_id,
        harness=harness,
        timeout=_EXIT_WAIT_S,
    )


@dataclass
class ArtifactQuarantine:
    """Move native files reversibly and quarantine test-created replacements."""

    root: Path
    moved: dict[Path, Path] = field(default_factory=dict)

    def hide(self, paths: list[Path]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(paths):
            backup = self.root / f"original-{index}-{source.name}"
            assert source.is_file(), f"native artifact disappeared before backup: {source}"
            shutil.move(str(source), backup)
            self.moved[source] = backup

    def restore(self) -> None:
        replacements = self.root / "reconstructed"
        replacements.mkdir(parents=True, exist_ok=True)
        for index, (source, backup) in enumerate(self.moved.items()):
            if source.exists():
                shutil.move(str(source), replacements / f"{index}-{source.name}")
            source.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                shutil.move(str(backup), source)

    def cleanup_test_artifacts(self, paths: set[Path]) -> None:
        cleanup = self.root / "final-test-artifacts"
        cleanup.mkdir(parents=True, exist_ok=True)
        for index, path in enumerate(sorted(paths)):
            if path.is_file():
                shutil.move(str(path), cleanup / f"{index}-{path.name}")


def run_native_compaction_resume_canary(
    *,
    harness: Harness,
    mode: ResumeMode,
    session_index: int,
    server: str,
    profile: str,
    tmp_path: Path,
) -> None:
    """Run one real compact, cold-resume, and exact marker-recall canary."""
    marker = f"OMNI-{harness.upper()}-{session_index}-{uuid.uuid4().hex[:12].upper()}"
    env = cli_env(profile=profile)
    config_home = Path(env["OMNIGENT_CONFIG_HOME"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = [str(omnigent_console_script()), harness, "--server", server]
    handles: list[PtyHandle] = []
    conversation_id: str | None = None
    external_session_id: str | None = None
    observed_paths: set[Path] = set()
    quarantine = ArtifactQuarantine(tmp_path / "native-artifact-quarantine")
    completed = False
    local_prefixes: dict[Path, bytes] = {}

    with httpx.Client(base_url=server, timeout=30.0) as client:
        try:
            fresh = spawn_cli_background(base, env=env, cwd=str(workspace))
            handles.append(fresh)
            conversation_id = wait_for_conversation_id(fresh, timeout=_START_WAIT_S)
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness=harness,
                timeout=_START_WAIT_S,
            )

            before_ack = _assistant_ids(client, conversation_id)
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=(
                    "Memorize this canary marker for later cold-resume recall. "
                    f"Reply with ONLY the exact marker: {marker}"
                ),
            )
            _wait_new_assistant_marker(
                client,
                conversation_id=conversation_id,
                marker=marker,
                previous_ids=before_ack,
            )
            external_session_id = poll_external_session_id(
                client, conversation_id=conversation_id, timeout=_START_WAIT_S
            )
            initial_paths = _wait_native_artifacts(
                client,
                harness=harness,
                conversation_id=conversation_id,
                external_session_id=external_session_id,
            )
            observed_paths.update(initial_paths)
            print(
                f"\n[{harness} canary] mode={mode} conversation_id={conversation_id} "
                f"external_session_id={external_session_id} marker={marker} "
                f"artifacts={[str(path) for path in initial_paths]}",
                flush=True,
            )

            compact_path, native_record, server_item = _compact_with_bounded_fill(
                client,
                harness=harness,
                conversation_id=conversation_id,
                external_session_id=external_session_id,
                marker=marker,
            )
            observed_paths.add(compact_path)
            _assert_compaction_mirror(
                harness,
                marker=marker,
                native_record=native_record,
                server_item=server_item,
            )
            print(
                f"[{harness} canary] compact_artifact={compact_path} "
                f"server_compaction_id={server_item.get('id')} "
                f"snapshot_source={server_item.get('snapshot_source', '<not exposed>')}",
                flush=True,
            )

            _clean_exit(
                fresh,
                client,
                conversation_id=conversation_id,
                harness=harness,
            )
            if mode == "server-reconstruction":
                paths_to_hide = _wait_native_artifacts(
                    client,
                    harness=harness,
                    conversation_id=conversation_id,
                    external_session_id=external_session_id,
                )
                quarantine.hide(paths_to_hide)
                assert not native_artifacts(
                    client,
                    harness=harness,
                    conversation_id=conversation_id,
                    external_session_id=external_session_id,
                ), "native artifact still visible; reconstruction path was not forced"
            else:
                local_prefixes = {
                    path: path.read_bytes()
                    for path in _wait_native_artifacts(
                        client,
                        harness=harness,
                        conversation_id=conversation_id,
                        external_session_id=external_session_id,
                    )
                }

            before_recall = _assistant_ids(client, conversation_id)
            resumed = spawn_cli_background(
                [*base, "--resume", conversation_id],
                env=env,
                cwd=str(workspace),
            )
            handles.append(resumed)
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness=harness,
                timeout=_START_WAIT_S,
            )
            resumed_paths = _wait_native_artifacts(
                client,
                harness=harness,
                conversation_id=conversation_id,
                external_session_id=external_session_id,
            )
            observed_paths.update(resumed_paths)
            if mode == "local-artifact":
                unchanged = [
                    path
                    for path, prefix in local_prefixes.items()
                    if path in resumed_paths and path.read_bytes().startswith(prefix)
                ]
                assert unchanged, (
                    "local-first resume rewrote or replaced every native artifact "
                    f"for {conversation_id}; expected append-only reuse"
                )
            elif harness == "codex":
                assert _native_compaction(harness, resumed_paths, marker) is not None, (
                    "server-reconstructed Codex rollout omitted the native compacted "
                    "replacement_history record"
                )
            print(
                f"[{harness} canary] cold_resume_artifacts="
                f"{[str(path) for path in resumed_paths]}",
                flush=True,
            )

            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=(
                    "What exact canary marker did you acknowledge before compaction? "
                    "Reply with ONLY that marker."
                ),
            )
            recalled = _wait_new_assistant_marker(
                client,
                conversation_id=conversation_id,
                marker=marker,
                previous_ids=before_recall,
            )
            assert marker in _assistant_text(recalled)
            _clean_exit(
                resumed,
                client,
                conversation_id=conversation_id,
                harness=harness,
            )
            completed = True
        finally:
            for handle in handles:
                if not handle.wait(0.01):
                    handle.terminate()
            quarantine.restore()
            if external_session_id is not None and conversation_id is not None:
                with contextlib.suppress(httpx.HTTPError):
                    observed_paths.update(
                        native_artifacts(
                            client,
                            harness=harness,
                            conversation_id=conversation_id,
                            external_session_id=external_session_id,
                        )
                    )
            if not completed and conversation_id is not None:
                diagnostics = tmp_path / "failed-native-canary-session.json"
                diagnostic_items: list[dict[str, Any]] = []
                with contextlib.suppress(httpx.HTTPError):
                    diagnostic_items = _server_items(client, conversation_id)
                diagnostics.write_text(
                    json.dumps(
                        {
                            "harness": harness,
                            "mode": mode,
                            "conversation_id": conversation_id,
                            "external_session_id": external_session_id,
                            "marker": marker,
                            "native_artifacts": [str(path) for path in sorted(observed_paths)],
                            "items": diagnostic_items,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                print(
                    f"[{harness} canary] failure diagnostics={diagnostics}; "
                    f"native_artifact_quarantine={quarantine.root}",
                    flush=True,
                )
            quarantine.cleanup_test_artifacts(observed_paths)
            try:
                if conversation_id is not None:
                    deleted = client.delete(
                        f"/v1/sessions/{conversation_id}",
                        timeout=30.0,
                    )
                    assert deleted.status_code in {200, 204, 404}, (
                        f"failed to delete canary session {conversation_id}: "
                        f"{deleted.status_code} {deleted.text}"
                    )
            finally:
                shutil.rmtree(config_home, ignore_errors=True)
