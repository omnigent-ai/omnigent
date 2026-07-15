"""Install ledger and uninstall backfill helpers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
LEDGER_NAME = "install_ledger.json"
BACKFILL_LEDGER_NAME = "install_ledger.backfill.json"
PROFILE_MARKER_BEGIN = "# >>> Omnigent installer >>>"
PROFILE_MARKER_END = "# <<< Omnigent installer <<<"
CONSOLE_SCRIPTS = ["omnigent", "omni"]


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def state_dir() -> Path:
    if data_dir := os.environ.get("OMNIGENT_DATA_DIR"):
        return Path(data_dir).expanduser()
    return Path.home() / ".omnigent"


def ledger_path() -> Path:
    return state_dir() / LEDGER_NAME


def backfill_ledger_path() -> Path:
    return state_dir() / BACKFILL_LEDGER_NAME


def platform_name() -> str:
    match platform.system():
        case "Darwin":
            return "macos"
        case "Linux":
            return "linux"
        case other:
            return other.lower() or "unknown"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class ProfileEntry:
    path: str
    marker_begin: str = PROFILE_MARKER_BEGIN
    marker_end: str = PROFILE_MARKER_END
    line_range: list[int] = field(default_factory=list)
    block_sha256: str | None = None
    content_matches_current: bool = True
    source: str = "recorded"
    confidence: str = "certain"


@dataclass
class ExternalConfigEntry:
    path: str
    marker: str
    format: str
    allowlist: list[str] = field(default_factory=list)
    block_sha256: str | None = None
    source: str = "recorded"
    confidence: str = "certain"


@dataclass
class DepEntry:
    present: bool
    path: str | None = None
    version: str | None = None
    installed_by: str = "unknown"
    confidence: str = "none"
    notes: str | None = None


@dataclass
class WheelEntry:
    installed: bool
    uv_tool_dir: str | None = None
    bin_dir: str | None = None
    console_scripts: list[str] = field(default_factory=lambda: CONSOLE_SCRIPTS.copy())
    source: str = "recorded"
    confidence: str = "certain"


@dataclass
class LaunchAgentEntry:
    kind: str
    path: str
    label: str
    source: str = "recorded"
    confidence: str = "high"


@dataclass
class StatePathsEntry:
    omnigent_home: str
    workspace: str
    desktop_data: list[str] = field(default_factory=list)


@dataclass
class LedgerEntries:
    profiles: list[ProfileEntry] = field(default_factory=list)
    injected_external_config: list[ExternalConfigEntry] = field(default_factory=list)
    deps: dict[str, DepEntry] = field(default_factory=dict)
    wheel: WheelEntry = field(default_factory=lambda: WheelEntry(installed=False))
    launch_agents: list[LaunchAgentEntry] = field(default_factory=list)
    state_paths: StatePathsEntry = field(
        default_factory=lambda: StatePathsEntry(
            omnigent_home=str(state_dir()), workspace=str(Path.home() / "omnigent")
        )
    )


@dataclass
class InstallLedger:
    schema_version: int
    ledger_source: str
    generator: dict[str, str]
    installation_id: str | None
    created_at: str
    updated_at: str
    last_validated_at: str
    entries: LedgerEntries

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstallLedger:
        entries_data = data.get("entries") or {}
        entries = LedgerEntries(
            profiles=[ProfileEntry(**item) for item in entries_data.get("profiles", [])],
            injected_external_config=[
                ExternalConfigEntry(**item)
                for item in entries_data.get("injected_external_config", [])
            ],
            deps={
                name: DepEntry(**value) for name, value in (entries_data.get("deps") or {}).items()
            },
            wheel=WheelEntry(**(entries_data.get("wheel") or {"installed": False})),
            launch_agents=[
                LaunchAgentEntry(**item) for item in entries_data.get("launch_agents", [])
            ],
            state_paths=StatePathsEntry(
                **(
                    entries_data.get("state_paths")
                    or {
                        "omnigent_home": str(state_dir()),
                        "workspace": str(Path.home() / "omnigent"),
                    }
                )
            ),
        )
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            ledger_source=str(data.get("ledger_source", "backfill")),
            generator=dict(data.get("generator") or {}),
            installation_id=data.get("installation_id"),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            last_validated_at=str(data.get("last_validated_at") or utc_now()),
            entries=entries,
        )


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def load_ledger(path: Path) -> InstallLedger | None:
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        return None
    if data.get("schema_version") != SCHEMA_VERSION:
        return None
    return InstallLedger.from_dict(data)


def write_ledger(ledger: InstallLedger, *, path: Path | None = None) -> None:
    atomic_write_json(path or ledger_path(), ledger.to_dict())


def _backfill_content_key(ledger: InstallLedger) -> dict[str, Any]:
    data = ledger.to_dict()
    for key in ("created_at", "updated_at", "last_validated_at"):
        data.pop(key, None)
    generator = data.get("generator")
    if isinstance(generator, dict):
        generator.pop("wrote_at", None)
    return data


def _merge_external_configs(
    current: list[ExternalConfigEntry], existing: list[ExternalConfigEntry]
) -> list[ExternalConfigEntry]:
    merged: dict[tuple[str, str, str], ExternalConfigEntry] = {}
    for entry in existing:
        merged[(entry.path, entry.marker, entry.format)] = entry
    for entry in current:
        merged[(entry.path, entry.marker, entry.format)] = entry
    return list(merged.values())


def read_installation_id(home: Path | None = None) -> str | None:
    install_id_path = (home or state_dir()) / "installation_id"
    try:
        value = install_id_path.read_text().strip()
    except OSError:
        return None
    return value or None


def profile_candidates() -> list[Path]:
    home = Path.home()
    candidates = [
        home / ".zprofile",
        home / ".zshrc",
        home / ".bash_profile",
        home / ".bashrc",
        home / ".profile",
        home / ".config" / "fish" / "config.fish",
    ]
    confd = home / ".config" / "fish" / "conf.d"
    if confd.is_dir():
        candidates.extend(sorted(confd.glob("*.fish")))
    return candidates


def find_profile_block(path: Path) -> tuple[int, int, str] | None:
    try:
        lines = path.read_text().splitlines(keepends=True)
    except OSError:
        return None
    begin: int | None = None
    for index, line in enumerate(lines):
        if line.rstrip("\n") == PROFILE_MARKER_BEGIN:
            begin = index
        elif begin is not None and line.rstrip("\n") == PROFILE_MARKER_END:
            block = "".join(lines[begin : index + 1])
            return begin + 1, index + 1, block
    return None


def _cmd_output(*args: str) -> str | None:
    try:
        result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or result.stderr.strip() or None


def _version_for(path: str | None) -> str | None:
    if not path:
        return None
    output = _cmd_output(path, "--version")
    if not output:
        return None
    return output.splitlines()[0]


def _dep(name: str, *, deep: bool, installed_by: str = "unknown") -> DepEntry:
    path = shutil.which(name) if deep else None
    return DepEntry(
        present=path is not None,
        path=path,
        version=_version_for(path) if deep else None,
        installed_by=installed_by if installed_by != "unknown" else "unknown",
        confidence="none" if installed_by == "unknown" else "certain",
    )


def _uv_tool_dir(*, bin_dir: bool = False) -> str | None:
    if not shutil.which("uv"):
        return None
    args = ["uv", "tool", "dir"] + (["--bin"] if bin_dir else [])
    return _cmd_output(*args)


def _wheel_entry(*, deep: bool, source: str, confidence: str) -> WheelEntry:
    bin_dir = _uv_tool_dir(bin_dir=True) if deep else None
    tool_dir = _uv_tool_dir(bin_dir=False) if deep else None
    installed = any(shutil.which(script) for script in CONSOLE_SCRIPTS) if deep else False
    if not installed and bin_dir:
        installed = any((Path(bin_dir) / script).exists() for script in CONSOLE_SCRIPTS)
    return WheelEntry(
        installed=installed,
        uv_tool_dir=tool_dir,
        bin_dir=bin_dir,
        console_scripts=CONSOLE_SCRIPTS.copy(),
        source=source,
        confidence=confidence if installed else "low",
    )


def desktop_data_paths() -> list[str]:
    home = Path.home()
    candidates: list[Path]
    if platform.system() == "Darwin":
        candidates = [
            home / "Library" / "Application Support" / "Omnigent",
            home / "Library" / "Caches" / "Omnigent",
            home / "Library" / "Logs" / "Omnigent",
        ]
    else:
        xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
        xdg_cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
        xdg_state = Path(os.environ.get("XDG_STATE_HOME", home / ".local" / "state"))
        candidates = [xdg_config / "Omnigent", xdg_cache / "Omnigent", xdg_state / "Omnigent"]
    return [str(path) for path in candidates if path.exists()]


def _json_has_key_path(path: Path, key_path: str) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    current: Any = data
    for part in key_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _toml_has_table(path: Path, table: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return any(line.strip() == f"[{table}]" for line in lines)


def observed_external_configs(*, deep: bool) -> list[ExternalConfigEntry]:
    if not deep:
        return []
    cwd = Path.cwd()
    candidates = [
        (cwd / ".cursor" / "mcp.json", "mcpServers.omnigent", "json"),
        (cwd / ".kiro" / "settings" / "mcp.json", "mcpServers.omnigent", "json"),
        (Path.home() / ".claude.json", "mcpServers.omnigent", "json"),
        (Path.home() / ".codex" / "config.toml", "mcp_servers.omnigent", "toml"),
    ]
    entries: list[ExternalConfigEntry] = []
    for path, marker, fmt in candidates:
        found = (
            _json_has_key_path(path, marker) if fmt == "json" else _toml_has_table(path, marker)
        )
        if found:
            entries.append(
                ExternalConfigEntry(
                    path=str(path),
                    marker=marker,
                    format=fmt,
                    allowlist=[marker],
                    source="observed",
                    confidence="certain",
                )
            )
    return entries


def observed_launch_agents(*, deep: bool) -> list[LaunchAgentEntry]:
    if not deep:
        return []
    entries: list[LaunchAgentEntry] = []
    launchd_dir = Path.home() / "Library" / "LaunchAgents"
    if launchd_dir.is_dir():
        for path in sorted(launchd_dir.glob("*omnigent*.plist")):
            entries.append(
                LaunchAgentEntry(
                    kind="launchd",
                    path=str(path),
                    label=path.stem,
                    source="observed",
                    confidence="high",
                )
            )
    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if systemd_dir.is_dir():
        for path in sorted(systemd_dir.glob("*omnigent*.service")):
            entries.append(
                LaunchAgentEntry(
                    kind="systemd_user",
                    path=str(path),
                    label=path.name,
                    source="observed",
                    confidence="high",
                )
            )
    return entries


def new_ledger(*, source: str, strategy: str, deep: bool) -> InstallLedger:
    now = utc_now()
    profiles: list[ProfileEntry] = []
    for path in profile_candidates():
        found = find_profile_block(path)
        if found is None:
            continue
        start, end, block = found
        profiles.append(
            ProfileEntry(
                path=str(path),
                line_range=[start, end],
                block_sha256=sha256_text(block),
                source="observed" if source == "backfill" else "recorded",
                confidence="certain",
            )
        )
    entries = LedgerEntries(
        profiles=profiles,
        injected_external_config=observed_external_configs(deep=deep),
        deps={name: _dep(name, deep=deep) for name in ("uv", "node", "npm", "tmux", "bwrap")},
        wheel=_wheel_entry(
            deep=deep, source="observed" if source == "backfill" else "recorded", confidence="high"
        ),
        launch_agents=observed_launch_agents(deep=deep),
        state_paths=StatePathsEntry(
            omnigent_home=str(state_dir()),
            workspace=str(Path.home() / "omnigent"),
            desktop_data=desktop_data_paths(),
        ),
    )
    return InstallLedger(
        schema_version=SCHEMA_VERSION,
        ledger_source=source,
        generator={
            "name": "omnigent",
            "version": _version_for(shutil.which("omnigent")) or "unknown",
            "strategy": strategy,
            "os": platform_name(),
            "wrote_at": now,
        },
        installation_id=read_installation_id(),
        created_at=now,
        updated_at=now,
        last_validated_at=now,
        entries=entries,
    )


def has_install_signal(ledger: InstallLedger) -> bool:
    return bool(
        ledger.installation_id or ledger.entries.profiles or ledger.entries.wheel.installed
    )


def backfill_install_ledger(*, deep: bool, apply: bool = True) -> InstallLedger | None:
    real = load_ledger(ledger_path())
    if real and real.ledger_source == "installer":
        return real

    strategy = "deep-backfill" if deep else "fast-backfill"
    ledger = new_ledger(source="backfill", strategy=strategy, deep=deep)
    if not has_install_signal(ledger):
        return None
    if apply:
        existing = load_ledger(backfill_ledger_path())
        if existing:
            if _backfill_content_key(existing) == _backfill_content_key(ledger):
                return existing
            ledger.created_at = existing.created_at
        write_ledger(ledger, path=backfill_ledger_path())
    return ledger


def resolve_uninstall_ledger() -> InstallLedger | None:
    real = load_ledger(ledger_path())
    if real and real.ledger_source == "installer":
        return real
    backfill = load_ledger(backfill_ledger_path())
    if backfill:
        return backfill
    return backfill_install_ledger(deep=True, apply=True)


def write_install_ledger_from_env() -> InstallLedger:
    existing = load_ledger(ledger_path())
    ledger = new_ledger(source="installer", strategy="install", deep=True)
    if existing and existing.ledger_source == "installer":
        ledger.created_at = existing.created_at
        ledger.entries.injected_external_config = _merge_external_configs(
            ledger.entries.injected_external_config, existing.entries.injected_external_config
        )
        ledger.entries.launch_agents = existing.entries.launch_agents
        for name, dep in existing.entries.deps.items():
            if name in ledger.entries.deps and dep.installed_by in {"omnigent", "preexisting"}:
                current = ledger.entries.deps[name]
                if current.installed_by == "unknown" or dep.installed_by == "omnigent":
                    current.installed_by = dep.installed_by
                    current.confidence = dep.confidence
    for name in list(ledger.entries.deps):
        env_name = f"OMNIGENT_LEDGER_DEP_{name.upper()}"
        installed_by = os.environ.get(env_name)
        if installed_by in {"omnigent", "preexisting", "unknown"}:
            ledger.entries.deps[name].installed_by = installed_by
            ledger.entries.deps[name].confidence = (
                "none" if installed_by == "unknown" else "certain"
            )
    profile_env = os.environ.get("OMNIGENT_LEDGER_PROFILE")
    if profile_env:
        path = Path(profile_env).expanduser()
        found = find_profile_block(path)
        ledger.entries.profiles = []
        if found is not None:
            start, end, block = found
            ledger.entries.profiles.append(
                ProfileEntry(
                    path=str(path),
                    line_range=[start, end],
                    block_sha256=sha256_text(block),
                    source="recorded",
                    confidence="certain",
                )
            )
    write_ledger(ledger)
    return ledger
