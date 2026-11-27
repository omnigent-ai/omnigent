"""Check native draft export, refusal preservation and conditional replacement."""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import (
    bridge,
    editor_control,
    editor_delegate,
    editor_handoff,
    editor_launch,
    editor_owner,
    editor_transaction,
    model_switch,
)
from omnigent.harnesses.claude_native.main import build_native_claude_terminal_env
from omnigent.runner.turn_routing import write_pending_replay, write_turn_routing_marker
from tests._helpers.native_claude import _PINNED_CLAUDE_MODELS
from tests._helpers.polling import poll_until
from tests.e2e.conftest import get_mock_message_requests

pytestmark = [pytest.mark.mock_only, pytest.mark.timeout(120)]


@pytest.mark.parametrize(
    "draft_mode",
    [
        "editor",
        "picker",
        "controller-clear",
        "launch-picker",
        "launch-controller-clear",
        "launch-paste-diagnostic",
        "launch-framed-paste-diagnostic",
    ],
)
@pytest.mark.parametrize("editor_source", ["configured", "configured-code", "default"])
def test_claude_external_editor_refuses_mismatches_without_mutating_the_draft(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    claude_managed_settings: None,
    record_property: Callable[[str, object], None],
    editor_source: str,
    draft_mode: str,
) -> None:
    claude, tmux_binary = shutil.which("claude"), shutil.which("tmux")
    assert claude is not None and tmux_binary is not None
    paste_diagnostic = draft_mode in {
        "launch-paste-diagnostic",
        "launch-framed-paste-diagnostic",
    }
    launch_model = (
        "draft-initial-model" if draft_mode != "editor" else _PINNED_CLAUDE_MODELS["sonnet"]
    )
    if paste_diagnostic:
        launch_model = _PINNED_CLAUDE_MODELS["sonnet"]
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "claude-config"
    config.mkdir()
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(
        bridge, "_BRIDGE_ROOT", tmp_path / f"omnigent-{os.getuid()}" / "claude-native"
    )
    bridge_dir = bridge.prepare_bridge_dir("editor-contract-conversation", workspace=workspace)
    instruction = tmp_path / "editor-instruction.json"
    observed = tmp_path / "editor-observed.jsonl"
    invoked = tmp_path / "editor-invoked.jsonl"
    hook_record = tmp_path / "blocked-prompt.json"
    owner_source = hashlib.sha256(Path(editor_owner.__file__).read_bytes()).hexdigest()
    handoff_source = hashlib.sha256(Path(editor_handoff.__file__).read_bytes()).hexdigest()
    control_source = hashlib.sha256(Path(editor_control.__file__).read_bytes()).hexdigest()
    transaction_source = hashlib.sha256(Path(editor_transaction.__file__).read_bytes()).hexdigest()
    process_context = (
        "import hashlib, os, psutil, shutil\n"
        "from dataclasses import asdict\n"
        "from omnigent.harnesses.claude_native import "
        "editor_control, editor_handoff, editor_owner, editor_transaction\n"
        f"native_executable = Path({str(Path(claude).resolve())!r})\n"
        "def native_process_record():\n"
        "    identity = editor_owner.observe_native_editor_process(native_executable)\n"
        "    return asdict(identity) if identity is not None else None\n"
        "def native_context():\n"
        "    parents = psutil.Process().parents()[:3]\n"
        "    return {'parents': [{'pid': p.pid, 'created': p.create_time(), "
        "'name': p.name(), 'exe': p.exe()} for p in parents], "
        "'owner_source_sha256': hashlib.sha256("
        "Path(editor_owner.__file__).read_bytes()).hexdigest(), "
        "'handoff_source_sha256': hashlib.sha256("
        "Path(editor_handoff.__file__).read_bytes()).hexdigest(), "
        "'control_source_sha256': hashlib.sha256("
        "Path(editor_control.__file__).read_bytes()).hexdigest(), "
        "'transaction_source_sha256': hashlib.sha256("
        "Path(editor_transaction.__file__).read_bytes()).hexdigest(), "
        "'editor_environment_sha256': hashlib.sha256(json.dumps("
        "{key: os.environ.get(key) for key in ('VISUAL', 'EDITOR', 'PATH')}, "
        "sort_keys=True).encode()).hexdigest(), "
        "'tty': os.ttyname(0) if os.isatty(0) else None, "
        "'session_env': {key: os.environ.get(key) for key in "
        "('CLAUDE_CODE_SESSION_ID', 'CLAUDE_SESSION_ID', "
        "'CLAUDE_CODE_PARENT_SESSION_ID', 'TMUX_PANE')}, "
        "'editor_env': {'visual_configured': bool(os.environ.get('VISUAL')), "
        "'editor_configured': bool(os.environ.get('EDITOR')), "
        "'path_head': os.environ.get('PATH', '').split(os.pathsep)[0], "
        "'code': shutil.which('code')}}\n"
    )
    hook = tmp_path / "block-prompt.py"
    hook.write_text(
        "import io, json, sys\n"
        "from pathlib import Path\n"
        "from omnigent.harnesses.claude_native import hook as native_hook\n"
        + process_context
        + f"destination = Path({str(hook_record)!r})\n"
        "payload = json.load(sys.stdin)\n"
        "sys.stdin = io.StringIO(json.dumps(payload))\n"
        f"native_hook.main(['--bridge-dir', {str(bridge_dir)!r}, "
        "'--native-editor-executable', str(native_executable)])\n"
        f"binding = editor_handoff.read_editor_bridge_binding(Path({str(bridge_dir)!r}), "
        "native_executable)\n"
        "temporary = destination.with_suffix('.tmp')\n"
        "record = {key: payload.get(key) for key in "
        "('prompt', 'session_id', 'hook_event_name')}\n"
        "record['native_context'] = native_context()\n"
        "record['native_process'] = native_process_record()\n"
        "record['owner_binding'] = asdict(binding) if binding is not None else None\n"
        "temporary.write_text(json.dumps(record))\n"
        "temporary.replace(destination)\n"
        "print(json.dumps({'decision': 'block', "
        "'reason': 'blocked-draft-ownership-probe'}))\n"
    )
    settings = tmp_path / "hook-settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": shlex.join([sys.executable, str(hook)]),
                                    "timeout": 15,
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    editor = tmp_path / "editor.py"
    saved_settings = config / "settings.json"
    saved_contents: bytes | None = None
    picker_observations: list[dict[str, object]] = []
    controller_observations: list[dict[str, object]] = []
    if draft_mode != "editor":
        value = json.loads(settings.read_text())
        value["modelPicker"] = {
            "replaceBuiltInOptions": True,
            "options": [
                {"model": launch_model, "label": "Draft initial"},
                {"model": "draft-test-model", "label": "Draft target"},
            ],
        }
        value["statusLine"] = bridge.build_hook_settings(
            bridge_dir, python_executable=sys.executable
        )["statusLine"]
        settings.write_text(json.dumps(value))
        saved_contents = json.dumps({"model": "draft-saved-default"}).encode()
        saved_settings.write_bytes(saved_contents)
        bridge.record_model_vocabulary(
            bridge_dir,
            launch_env=None,
            launch_model=launch_model,
            picker_values=[launch_model, "draft-test-model"],
            picker_options=[
                {
                    "value": identity,
                    "model": identity,
                    "display_name": label,
                    "enabled": True,
                    "provenance": "structured",
                }
                for identity, label in (
                    (launch_model, "Draft initial"),
                    ("draft-test-model", "Draft target"),
                )
            ],
        )
    editor.write_text(
        "import json, re, sys\n"
        "from pathlib import Path\n"
        + process_context
        + f"instruction = Path({str(instruction)!r})\n"
        f"observed = Path({str(observed)!r})\n"
        f"invoked = Path({str(invoked)!r})\n"
        "program = sys.argv[sys.argv.index('--editor-program') + 1]\n"
        "with invoked.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'program': program, "
        "'arguments': [arg if arg.startswith('-') else Path(arg).name "
        "for arg in sys.argv[sys.argv.index('--editor-program') + 2:]]}) + '\\n')\n"
        "location = sys.argv[-1]\n"
        "if program in ('code', 'code-insiders') and "
        "any(flag in sys.argv for flag in ('-g', '--goto')):\n"
        "    location = re.sub(r':\\d+(?::\\d+)?$', '', location)\n"
        "source = Path(location)\n"
        "content = source.read_bytes().decode('utf-8')\n"
        "operation = editor_control.run_editor_operation(source, "
        f"bridge_dir=Path({str(bridge_dir)!r}), "
        "executable=native_executable)\n"
        "if operation is not None:\n"
        "    with observed.open('a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps({'kind': 'controller', 'content': content, "
        "'after': source.read_bytes().decode('utf-8'), 'accepted': operation == 0, "
        "'program': program, 'native_context': native_context()}) + '\\n')\n"
        "    sys.exit(operation)\n"
        "request = json.loads(instruction.read_text())\n"
        "def current_owner():\n"
        f"    binding = editor_handoff.read_editor_bridge_binding(Path({str(bridge_dir)!r}), "
        "native_executable)\n"
        "    return binding.owner if binding is not None else None\n"
        "claimed = request['owner']\n"
        "expected_owner = editor_owner.NativeDraftOwner("
        "claimed['bridge_session_id'], claimed['native_session_id'], "
        "editor_owner.NativeEditorProcess(**claimed['process']))\n"
        "accepted = editor_owner.exported_draft_matches_owner(source, "
        "expected_text=request['expected'], expected_owner=expected_owner, "
        "current_owner=current_owner, cancelled=lambda: request['cancelled'])\n"
        "with observed.open('a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps({'kind': 'manual', 'content': content, 'accepted': accepted, "
        "'observe_only': request.get('observe_only', False), "
        "'program': program, 'native_context': native_context()}) + '\\n')\n"
        "if not accepted or request.get('observe_only', False):\n"
        "    sys.exit(1)\n"
        "source.write_bytes(request['replacement'].encode('utf-8'))\n"
    )
    editor_command = shlex.join([sys.executable, str(editor)])
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "TERM": "xterm-256color",
        "TMPDIR": str(tmp_path),
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": str(config),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": isolated_mock_llm_server_url,
        "ANTHROPIC_AUTH_TOKEN": "mock-key",
        "DISABLE_AUTOUPDATER": "1",
        "EDITOR": editor_command + " --editor-program editor",
        "VISUAL": editor_command + " --editor-program visual",
    }
    if paste_diagnostic:
        env.update(build_native_claude_terminal_env(None, client_surface="cli"))
    if editor_source in {"default", "configured-code"}:
        if editor_source == "default":
            env.pop("EDITOR")
            env.pop("VISUAL")
        programs = tmp_path / "editors"
        programs.mkdir()
        for program in (
            "vi",
            "vim",
            "nano",
            "emacs",
            "nvim",
            "code",
            "code-insiders",
            "cursor",
            "zed",
            "subl",
            "editor",
            "sensible-editor",
        ):
            launcher = programs / program
            launcher.write_text(
                "#!/bin/sh\nexec "
                + shlex.join([sys.executable, str(editor), "--editor-program", program])
                + ' "$@"\n'
            )
            launcher.chmod(0o700)
        env["PATH"] = str(programs) + os.pathsep + env["PATH"]
        if editor_source == "configured-code":
            env["EDITOR"] = env["VISUAL"] = shlex.join([str(programs / "code"), "--reuse-window"])
    original_editor_environment = {key: env.get(key) for key in ("VISUAL", "EDITOR", "PATH")}
    editor_environment_digest = hashlib.sha256(
        json.dumps(original_editor_environment, sort_keys=True).encode()
    ).hexdigest()
    delegate_manifests: list[tuple[Path, list[str]]] = []
    delegate_prefix: str | None = None

    def delegate_command(command: list[str], name: str) -> list[str]:
        manifest = tmp_path / f"delegate-{name}.json"
        delegate_manifests.append((manifest, command))
        return [sys.executable, "-m", editor_delegate.__name__, "--manifest", str(manifest), "--"]

    if draft_mode.startswith("launch-"):
        overrides = editor_launch.prepare_editor_environment(
            bridge_dir, executable=Path(claude), environment=env
        )
        assert overrides is not None
        env.update(overrides)
    elif editor_source == "configured":
        for key, program in (("VISUAL", "visual"), ("EDITOR", "editor")):
            env[key] = shlex.join(
                delegate_command(
                    [sys.executable, str(editor), "--editor-program", program], program
                )
            )
    elif editor_source == "configured-code":
        delegates = tmp_path / "configured-editor"
        delegates.mkdir()
        launcher = delegates / "code"
        launcher.write_text(
            "#!/bin/sh\nexec "
            + shlex.join(delegate_command([str(programs / "code")], "code"))
            + ' "$@"\n'
        )
        launcher.chmod(0o700)
        env["EDITOR"] = env["VISUAL"] = shlex.join([str(launcher), "--reuse-window"])
    else:
        delegates = tmp_path / "delegated-editors"
        delegates.mkdir()
        delegate_prefix = str(delegates)
        for original_program in sorted(programs.iterdir()):
            program = original_program.name
            launcher = delegates / program
            launcher.write_text(
                "#!/bin/sh\nexec "
                + shlex.join(
                    delegate_command(
                        [sys.executable, str(editor), "--editor-program", program], program
                    )
                )
                + ' "$@"\n'
            )
            launcher.chmod(0o700)
        env["PATH"] = str(delegates) + os.pathsep + env["PATH"]
    replaced_keys = ("VISUAL", "EDITOR") if editor_source != "default" else ()
    for manifest, command in delegate_manifests:
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "command": command,
                    "restore_environment": {
                        key: original_editor_environment[key] for key in replaced_keys
                    },
                    "injected_environment": {key: env[key] for key in replaced_keys},
                    "path_prefix": delegate_prefix,
                }
            )
        )
        manifest.chmod(0o600)
    bridge.ensure_claude_workspace_trusted(workspace, env)
    native_feature_profile: dict[str, object] | None = None
    if draft_mode == "launch-framed-paste-diagnostic":
        flag = "tengu_virtual_pancake"
        native_binary = Path(claude).resolve()
        with (
            native_binary.open("rb") as stream,
            mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as source,
        ):
            assert source.find(flag.encode()) >= 0 and source.find(b"pasted_content") >= 0
        with native_binary.open("rb") as stream:
            binary_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        native_config = bridge.claude_global_config_path(cwd=workspace, env=env)
        assert native_config is not None and native_config.parent == config
        global_config = json.loads(native_config.read_text())
        now_ms = int(time.time() * 1000)
        for key in ("cachedFeatureFlags", "cachedGrowthBookFeatures"):
            current = global_config.get(key, {})
            assert isinstance(current, dict)
            global_config[key] = {**current, flag: True}
            global_config[key + "At"] = now_ms
        native_config.write_text(json.dumps(global_config))
        native_feature_profile = {
            "flag": flag,
            "fixture_value": True,
            "binary_sha256": binary_digest,
            "config_path": str(native_config),
            "diagnostic_only": True,
        }
    version = subprocess.check_output([claude, "--version"], env=env, text=True, timeout=10)
    if native_feature_profile is not None:
        assert version.strip() == "2.1.323 (Claude Code)"
    typed_draft = (
        "  editor-wrapped-draft "
        + "0123456789 " * 24
        + "draft-end\n\nsecond line with spaces  \nthird line 😀\n"
    )
    if paste_diagnostic:
        prefix = (
            "This is turn 1. Context marker native-paste-draft-end. "
            "Reply with exactly this token and nothing else: native-paste-reply\n\n"
        )
        typed_draft = prefix + ("wrapped continuation text " * 20)[: 336 - len(prefix)] + "\n"
        assert len(typed_draft) == 337

    def observations() -> list[dict[str, object]]:
        if not observed.exists():
            return []
        try:
            return [json.loads(line) for line in observed.read_text().splitlines()]
        except json.JSONDecodeError:
            return []

    with tempfile.TemporaryDirectory(prefix="omni-editor-") as socket_dir:
        tmux = [tmux_binary, "-S", str(Path(socket_dir) / "tmux.sock")]
        target = "editor:0.0"

        def command(*args: str, text: str | None = None) -> None:
            subprocess.run([*tmux, *args], input=text, text=True, check=True, timeout=10)

        last_pane = ""

        def pane() -> str:
            nonlocal last_pane
            last_pane = subprocess.check_output(
                [*tmux, "capture-pane", "-p", "-t", target], text=True, timeout=5
            )
            return last_pane

        def wait_for_editor(count: int, expected: str) -> None:
            poll_until(lambda: len(observations()) == count + 1, timeout_s=15)
            poll_until(
                lambda: (
                    model_switch._draft(pane()) == ""
                    if expected == ""
                    else bridge._draft_in_input_box(pane(), expected.splitlines()[0].strip())
                ),
                timeout_s=10,
            )

        def controlled_edit(expected: str, replacement: str, *, cancel_after: str | None = None):
            binding = editor_handoff.read_controller_editor_binding(bridge_dir)
            assert binding is not None
            cancelled = threading.Event()
            control = editor_control.NativeEditorControl(
                bridge_dir,
                binding=binding,
                expected_text=expected,
                replacement_text=replacement,
                cancelled=cancelled.is_set,
                deadline=time.monotonic() + 15,
            )
            receive = editor_control._Channel.receive

            def received(channel, phase):
                result = receive(channel, phase)
                if phase == cancel_after:
                    cancelled.set()
                return result

            with (
                monkeypatch.context() as changes,
                bridge._bridge_injection_lock(bridge_dir),
                bridge._bridge_injection_file_lock(bridge_dir),
            ):
                changes.setattr(editor_control._Channel, "receive", received)
                return control.run(lambda: command("send-keys", "-t", target, "C-g"))

        try:
            subprocess.run(
                [
                    *tmux,
                    "-f",
                    os.devnull,
                    "new-session",
                    "-d",
                    "-s",
                    "editor",
                    "-x",
                    "100",
                    "-y",
                    "50",
                    "-c",
                    str(workspace),
                    claude,
                    "--settings",
                    str(settings),
                    "--model",
                    launch_model,
                    "--strict-mcp-config",
                    "--mcp-config",
                    '{"mcpServers":{}}',
                ],
                env=env,
                check=True,
                timeout=10,
            )
            bridge.write_tmux_target(
                bridge_dir, socket_path=Path(socket_dir) / "tmux.sock", tmux_target=target
            )
            try:
                poll_until(lambda: bridge._composer_box(pane()) is not None, timeout_s=30)
            except AssertionError as exc:
                raise AssertionError({"phase": "initial-composer", "pane": pane()}) from exc
            command("load-buffer", "-b", "editor-draft", "-", text=typed_draft)
            command("paste-buffer", "-d", "-p", "-b", "editor-draft", "-t", target)
            if paste_diagnostic:
                time.sleep(1.0)
                picker_observations.append({"kind": "settled-paste-before-submit", "pane": pane()})
            else:
                poll_until(lambda: bridge._draft_in_input_box(pane(), "draft-end"), timeout_s=10)
            command("send-keys", "-t", target, "Enter")
            poll_until(hook_record.exists, timeout_s=15)
            poll_until(
                lambda: (
                    "blocked-draft-ownership-probe" in pane()
                    and (paste_diagnostic or bridge._draft_in_input_box(last_pane, "draft-end"))
                ),
                timeout_s=10,
            )
            payload = json.loads(hook_record.read_text())
            draft = payload["prompt"]
            assert payload["hook_event_name"] == "UserPromptSubmit"
            assert isinstance(draft, str) and "\n" in draft and payload["session_id"]
            assert payload["native_process"] is not None, payload["native_context"]
            assert payload["native_context"]["owner_source_sha256"] == owner_source
            assert payload["native_context"]["handoff_source_sha256"] == handoff_source
            assert payload["owner_binding"] is not None, payload
            owner = payload["owner_binding"]["owner"]
            assert owner["bridge_session_id"] == bridge.read_active_session_id(bridge_dir)
            assert owner["native_session_id"] == payload["session_id"]
            assert owner["process"] == payload["native_process"]
            if paste_diagnostic:
                assert "<pasted_content" in draft, "The diagnostic did not reach a collapsed paste"
                picker_observations.append({"kind": "before-observation", "pane": pane()})
                count = len(observations())
                instruction.write_text(
                    json.dumps(
                        {
                            "expected": draft,
                            "replacement": "must-never-be-imported",
                            "owner": owner,
                            "cancelled": False,
                            "observe_only": True,
                        }
                    )
                )
                command("send-keys", "-t", target, "C-g")
                poll_until(lambda: len(observations()) == count + 1, timeout_s=15)
                assert observations()[-1]["observe_only"] is True
                if draft_mode == "launch-framed-paste-diagnostic":
                    poll_until(lambda: model_switch._draft(pane()) is not None, timeout_s=10)
                    assert json.loads(hook_record.read_text()) == payload
                    current_binding = editor_handoff.read_controller_editor_binding(bridge_dir)
                    assert current_binding is not None and asdict(current_binding.owner) == owner
                    assert saved_settings.read_bytes() == saved_contents
                    assert get_mock_message_requests(isolated_mock_llm_server_url) == []
                    assert not controller_observations
                    return
                session_id = bridge.read_active_session_id(bridge_dir)
                assert session_id is not None
                assert write_turn_routing_marker(bridge_dir, session_id=session_id)
                assert write_pending_replay(
                    bridge_dir,
                    session_id=session_id,
                    prompt=draft,
                    blocked_turn_id=None,
                    model="draft-test-model",
                )
                try:
                    result = bridge.inject_model_command(
                        bridge_dir, model="draft-test-model", owned_draft=draft, timeout_s=30
                    )
                    controller_observations.append({"kind": "selected", "receipt": asdict(result)})
                except model_switch.ClaudeModelSwitchRefused as exc:
                    controller_observations.append(
                        {"kind": "refused", "phase": exc.phase, "message": str(exc)}
                    )
                assert saved_settings.read_bytes() == saved_contents
                assert get_mock_message_requests(isolated_mock_llm_server_url) == []
                return
            if draft_mode.endswith("controller-clear"):
                session_id = bridge.read_active_session_id(bridge_dir)
                assert session_id is not None
                assert write_turn_routing_marker(bridge_dir, session_id=session_id)
                wrong = draft + "changed-claim"
                assert write_pending_replay(
                    bridge_dir,
                    session_id=session_id,
                    prompt=wrong,
                    blocked_turn_id=None,
                    model="draft-test-model",
                )
                count = len(observations())
                with pytest.raises(model_switch.ClaudeModelSwitchRefused) as refusal:
                    bridge.inject_model_command(
                        bridge_dir, model="draft-test-model", owned_draft=wrong, timeout_s=30
                    )
                if draft_mode.startswith("launch-"):
                    assert len(observations()) == count
                else:
                    wait_for_editor(count, draft)
                    refused = observations()[-1]
                    assert refused["kind"] == "controller" and refused["accepted"] is False
                    assert refused["content"] == refused["after"] == draft
                controller_observations.append(
                    {
                        "kind": "refused",
                        "phase": refusal.value.phase,
                        "model_may_have_changed": refusal.value.model_may_have_changed,
                    }
                )
                assert not (bridge_dir / "editor-operation.json").exists()
                count = len(observations())
                instruction.write_text(
                    json.dumps(
                        {
                            "expected": draft,
                            "replacement": "must-never-be-imported",
                            "owner": owner,
                            "cancelled": False,
                            "observe_only": True,
                        }
                    )
                )
                command("send-keys", "-t", target, "C-g")
                wait_for_editor(count, draft)
                assert observations()[-1]["content"] == draft
                assert observations()[-1]["accepted"] is True
                assert write_pending_replay(
                    bridge_dir,
                    session_id=session_id,
                    prompt=draft,
                    blocked_turn_id=None,
                    model="draft-test-model",
                )
                count = len(observations())
                result = bridge.inject_model_command(
                    bridge_dir, model="draft-test-model", owned_draft=draft, timeout_s=30
                )
                assert isinstance(result, model_switch.ClaudeModelSwitchResult)
                controller_observations.append({"kind": "selected", "receipt": asdict(result)})
                if draft_mode.startswith("launch-"):
                    poll_until(lambda: model_switch._draft(pane()) == "", timeout_s=10)
                    assert len(observations()) == count
                else:
                    wait_for_editor(count, "")
                    cleared = observations()[-1]
                    assert cleared["kind"] == "controller" and cleared["accepted"] is True
                    assert cleared["content"] == draft and cleared["after"] == ""
                assert result.resolved_model == result.status_model == "draft-test-model"
                assert result.session_id == payload["session_id"]
                assert not (bridge_dir / "editor-operation.json").exists()
                assert saved_settings.read_bytes() == saved_contents
                assert get_mock_message_requests(isolated_mock_llm_server_url) == []
                return
            if draft_mode in {"picker", "launch-picker"}:

                def raw_status() -> dict[str, object]:
                    return bridge._read_json_file(bridge_dir / bridge.CONTEXT_RAW_FILE)

                def wait_status(model: str, *, after: int | None = None) -> None:
                    def matching() -> bool:
                        current = raw_status()
                        native_model = current.get("model")
                        return (
                            current.get("session_id") == payload["session_id"]
                            and current.get("version") == "2.1.323"
                            and isinstance(native_model, dict)
                            and native_model.get("id") == model
                            and (
                                after is None
                                or (bridge_dir / bridge.CONTEXT_RAW_FILE).stat().st_mtime_ns
                                > after
                            )
                        )

                    poll_until(matching, timeout_s=15)
                    picker_observations.append(
                        {
                            "kind": "status",
                            "raw": raw_status(),
                            "after_ns": after,
                            "mtime_ns": (bridge_dir / bridge.CONTEXT_RAW_FILE).stat().st_mtime_ns,
                        }
                    )

                def live_picker():
                    rendered = pane()
                    return model_switch._picker(rendered)

                def open_picker() -> None:
                    command("send-keys", "-t", target, "M-p")
                    poll_until(lambda: live_picker() is not None, timeout_s=10)

                def choose(label: str, *, commit: bool) -> None:
                    open_picker()
                    for _ in range(4):
                        current = live_picker()
                        assert current is not None
                        rows = [
                            row for row in current.rows if model_switch._row_label(row) == label
                        ]
                        assert len(rows) == 1, current
                        if current.selected.number == rows[0].number:
                            break
                        direction = "Up" if current.selected.number > rows[0].number else "Down"
                        command("send-keys", "-t", target, direction)
                        poll_until(
                            lambda current=current: (
                                (new := live_picker()) is not None
                                and new.selected != current.selected
                            ),
                            timeout_s=5,
                        )
                    else:
                        pytest.fail("Could not select the declared live picker row")
                    picker_observations.append(
                        {"kind": "picker", "label": label, "pane": pane(), "commit": commit}
                    )
                    command("send-keys", "-t", target, "s" if commit else "Escape")
                    poll_until(lambda: live_picker() is None, timeout_s=10)

                def observe_without_import() -> None:
                    count = len(observations())
                    instruction.write_text(
                        json.dumps(
                            {
                                "expected": draft,
                                "replacement": "must-never-be-imported",
                                "owner": owner,
                                "cancelled": False,
                                "observe_only": True,
                            }
                        )
                    )
                    command("send-keys", "-t", target, "C-g")
                    wait_for_editor(count, draft)
                    observed_draft = observations()[-1]
                    assert observed_draft["content"] == draft
                    assert observed_draft["accepted"] is True
                    assert observed_draft["observe_only"] is True
                    assert (
                        observed_draft["native_context"]["editor_environment_sha256"]
                        == editor_environment_digest
                    )

                wait_status(launch_model)
                before = (bridge_dir / bridge.CONTEXT_RAW_FILE).stat().st_mtime_ns
                choose("Draft target", commit=True)
                wait_status("draft-test-model", after=before)
                observe_without_import()
                open_picker()
                current = live_picker()
                assert current is not None
                checked = [row for row in current.rows if model_switch._row_is_current(row)]
                assert len(checked) == 1 and model_switch._row_label(checked[0]) == "Draft target"
                picker_observations.append({"kind": "current", "pane": pane()})
                command("send-keys", "-t", target, "Escape")
                poll_until(lambda: live_picker() is None, timeout_s=10)
                observe_without_import()
                choose("Draft initial", commit=False)
                wait_status("draft-test-model")
                observe_without_import()
                before = (bridge_dir / bridge.CONTEXT_RAW_FILE).stat().st_mtime_ns
                choose("Draft initial", commit=True)
                wait_status(launch_model, after=before)
                observe_without_import()
                assert saved_settings.read_bytes() == saved_contents
                assert get_mock_message_requests(isolated_mock_llm_server_url) == []
                return
            for phase in ("prepared", "committed"):
                count = len(observations())
                with pytest.raises(editor_transaction.NativeDraftEditRefused):
                    controlled_edit(draft, "", cancel_after=phase)
                wait_for_editor(count, draft)
                assert observations()[-1]["content"] == observations()[-1]["after"] == draft
                assert observations()[-1]["accepted"] is False
                assert not (bridge_dir / "editor-operation.json").exists()
            count = len(observations())
            receipt = controlled_edit(draft, "")
            wait_for_editor(count, "")
            assert receipt.original_text == draft and receipt.replacement_text == ""
            assert observations()[-1]["content"] == draft and observations()[-1]["after"] == "\n"
            count = len(observations())
            controlled_edit(receipt.replacement_text, receipt.original_text)
            wait_for_editor(count, draft)
            assert (
                observations()[-1]["content"] == "" and observations()[-1]["after"] == draft + "\n"
            )
            wrong_session = {**owner, "native_session_id": "other-native-session"}
            wrong_process = {
                **owner,
                "process": {
                    **payload["native_process"],
                    "created_at": payload["native_process"]["created_at"] + 1,
                },
            }
            steps = [
                (
                    draft[:-1] if draft.endswith("\n") else draft + "\n",
                    draft,
                    False,
                    owner,
                    False,
                ),
                (draft.replace("\n", " "), draft, False, owner, False),
                (draft, draft, False, wrong_session, False),
                (draft, draft, False, wrong_process, False),
                (draft, draft, False, owner, True),
                (draft, draft, True, owner, False),
                ("unrelated claim", "/status", False, owner, False),
            ]
            for index, (claimed, expected, accepted, expected_owner, cancelled) in enumerate(
                steps
            ):
                count = len(observations())
                instruction.write_text(
                    json.dumps(
                        {
                            "expected": claimed,
                            "replacement": "/status",
                            "owner": expected_owner,
                            "cancelled": cancelled,
                        }
                    )
                )
                command("send-keys", "-t", target, "C-g")
                poll_until(lambda count=count: len(observations()) == count + 1, timeout_s=15)
                observation = observations()[-1]
                actual = observation["content"]
                assert actual == expected, {"step": index, "expected": expected, "actual": actual}
                assert observation["accepted"] is accepted
                assert observation["native_context"]["owner_source_sha256"] == owner_source
                assert observation["native_context"]["handoff_source_sha256"] == handoff_source
                assert (
                    observation["native_context"]["editor_environment_sha256"]
                    == editor_environment_digest
                )
                poll_until(
                    lambda expected=("/status" if accepted else expected): (
                        bridge._composer_box(pane()) is not None
                        and bridge._draft_in_input_box(last_pane, expected.splitlines()[0].strip())
                    ),
                    timeout_s=10,
                )
            count = len(observations())
            trailing = "  exact suffix \t\n\n"
            controlled_edit("/status", trailing)
            wait_for_editor(count, trailing)
            count = len(observations())
            receipt = controlled_edit(trailing, "")
            wait_for_editor(count, "")
            changed = "new-user-edit-preserved"
            command("send-keys", "-l", "-t", target, changed)
            poll_until(lambda: bridge._draft_in_input_box(pane(), changed), timeout_s=10)
            count = len(observations())
            with pytest.raises(editor_transaction.NativeDraftEditRefused):
                controlled_edit(receipt.replacement_text, receipt.original_text)
            wait_for_editor(count, changed)
            assert observations()[-1]["content"] == observations()[-1]["after"] == changed
            assert observations()[-1]["accepted"] is False
            assert all(
                item["native_context"]["control_source_sha256"] == control_source
                and item["native_context"]["transaction_source_sha256"] == transaction_source
                for item in observations()
            )
            if editor_source == "configured-code":
                invocations = [json.loads(line) for line in invoked.read_text().splitlines()]
                assert all("--reuse-window" in item["arguments"] for item in invocations)
                assert all(
                    "-g" in item["arguments"] or "--goto" in item["arguments"]
                    for item in invocations
                )
            assert get_mock_message_requests(isolated_mock_llm_server_url) == []
        finally:
            try:
                final_pane_error = None
                try:
                    pane()
                except (OSError, subprocess.SubprocessError) as exc:
                    final_pane_error = type(exc).__name__
                if native_feature_profile is not None:
                    current_native_config = json.loads(native_config.read_text())
                    native_feature_profile["saved_values_after_observation"] = {
                        key: current_native_config.get(key, {}).get(flag)
                        for key in ("cachedFeatureFlags", "cachedGrowthBookFeatures")
                    }
                diagnostics = {
                    "version": version.strip(),
                    "editor_source": editor_source,
                    "draft_mode": draft_mode,
                    "native_feature_profile": native_feature_profile,
                    "picker_observations": picker_observations,
                    "controller_observations": controller_observations,
                    "model_switch_source_sha256": hashlib.sha256(
                        Path(model_switch.__file__).read_bytes()
                    ).hexdigest(),
                    "editor_launch_source_sha256": hashlib.sha256(
                        Path(editor_launch.__file__).read_bytes()
                    ).hexdigest(),
                    "saved_model_settings_unchanged": saved_settings.read_bytes() == saved_contents
                    if saved_contents is not None
                    else None,
                    "editor_delegation_source_sha256": hashlib.sha256(
                        Path(editor_delegate.__file__).read_bytes()
                    ).hexdigest(),
                    "expected_editor_environment_sha256": editor_environment_digest,
                    "typed_draft": typed_draft,
                    "blocked_hook": json.loads(hook_record.read_text())
                    if hook_record.exists()
                    else None,
                    "editor_observations": observations(),
                    "editor_invocations": [
                        json.loads(line) for line in invoked.read_text().splitlines()
                    ]
                    if invoked.exists()
                    else [],
                    "last_pane": last_pane,
                    "final_pane_error": final_pane_error,
                    "provider_messages": len(
                        get_mock_message_requests(isolated_mock_llm_server_url)
                    ),
                }
                record_property("editor_contract", json.dumps(diagnostics))
                print(json.dumps(diagnostics))
            finally:
                subprocess.run(
                    [*tmux, "kill-server"], capture_output=True, check=False, timeout=10
                )
