# Non-HTTP Credential Broker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a sandboxed omnigent agent use non-HTTP credentialed CLIs (`psql`, DB-backed `pytest`) and credentialed MCP servers without the secret entering the agent's **ambient** env or any readable file — resolved in the trusted parent (preferring ephemeral), handed to one specific tool invocation on demand, and authenticated per-handle so a different same-uid agent can't steal it.

**Architecture:** A parent-side broker holds a loaded-at-unlock key store + a tool→credential-group map + a per-handle token. Agent-controlled surfaces (os_env shell; native terminals) get a shim directory prepended to `PATH`; each shim execs a **self-contained, stdlib-only** client that reads a per-handle token from its inherited env, fetches the tool's credentials over an `AF_UNIX` socket in the bound scratch dir, and execs the real tool with the creds in its env. MCP-server launches (parent-side, unsandboxed) merge resolved creds into the spawn env. Mirrors the existing `credential_proxy` (parent-side-only `SandboxPolicy` field) and egress `controller`/auth-token patterns.

**Tech stack:** Python 3.12, `socket` AF_UNIX, `dataclasses`, Pydantic boundary models (spec parsing), threads, pytest (`asyncio_mode="auto"`).

---

## Security model (state these guarantees honestly; do not overclaim)

- **`fallback: command` (ephemeral) fields:** the long-lived secret NEVER enters the sandbox — only a short-lived, audience-scoped token crosses, per-invocation, to the one mapped tool. **Use for real secrets.**
- **`load:`-store fields:** the loaded value (which MAY be a long-lived secret) enters ONLY the specific tool's process per invocation — NOT the agent's ambient env. Strictly better than `env_passthrough` (no session-long ambient credential + audited) but NOT "never in the sandbox." **Reserve for non-secret config (PGHOST/PGPORT) or where ephemeral resolution is impossible.**
- **Per-handle token:** the broker requires a 256-bit token delivered to the helper via the config FD and injected into the helper's in-process `os.environ` (invisible in the execve `/proc/<pid>/environ` snapshot, exactly like `egress_auth_token`). The shim's client reads it from its inherited env. Other same-uid HOST processes (incl. a different concurrent agent) cannot read the in-process env → cannot forge the token → no cross-agent credential theft / audit misattribution. The agent's own sibling shells inherit it (admitted replay — accepted).
- uid + `0600` socket in a `0700` dir stops other LOCAL USERS; `ptrace`/`process_vm_readv` are blocked by the seccomp denylist. **Accepted residual:** a concurrent `/proc/<pid>/environ` read of the running tool, bounded by ephemerality. Full elimination would require the parent to spawn the tool `nsenter`'d into the namespace — deferred.

## Constraints (enforced at parse time)
`credential_broker` requires `allow_network: true`, requires `sandbox.type in {linux_bwrap, darwin_seatbelt}`, and is **incompatible with `egress_rules`** (HTTP-only egress isolates the network and would block brokered tools' raw TCP).

## Verified anchors (current as of plan date)
- os_env.py: `_start_locked`@398, `build_helper_env`@154, `env`@400, `if sandbox.active:`@411, tmpdir@412, write-root@413, credential_proxy block@418-434, config dict@449, `egress_auth_token` config@464 + in-process inject@1427-1442, Popen `env=env`@519, `_stop_locked`@555-587, `__init__`@339, `_shell_impl` (no `env=`)@1282.
- sandbox.py: `credential_proxy` field@157 (NOT in `to_jsonable`/`from_jsonable`), `_clone_policy_with`@407, `with_additional_write_roots`@416, `create_private_tmpdir`@548, `run_launcher` `subprocess.run`@703.
- **Bridge sites:** `bwrap_sandbox.py:278` and `seatbelt_sandbox.py:441` (where `credential_proxy=sandbox_spec.credential_proxy` is copied spec→policy inside `resolve()`).
- datamodel.py: `CredentialSourceSpec`@388, `CredentialProxyEntry`@397, `credential_proxy`@658; `Literal`/`field` imported.
- parser.py: Pydantic `_CredentialSourceModel`@1102, `.to_spec()`@1150, `_CredentialProxyItemModel`@1166, `ConfigDict(extra="forbid")`@1119, `_parse_credential_proxy`@1285, `_parse_os_env_sandbox`@763 + constructor@852.
- loader.py: `_parse_os_env_sandbox_spec`@738, parser import-reuse@787, constructor@816, `_effective_terminal_sandbox`@689-703 (broker spec reachable via `agent.os_env.sandbox.credential_broker`).
- seatbelt_sandbox.py: `(allow network*)`@1024 (covers AF_UNIX connect when `allow_network=true`).
- bwrap_sandbox.py: `_DEFAULT_RO_DIRS`@106 (no `/opt/homebrew`), `_ensure_executable_visible`@708.
- terminal.py: dataclass fields `_egress_handle`/`_egress_tmpdir`@781-782, `self.sandbox_policy`@749, launch env@892-909, `if sandbox_for_launcher is not None and ...active:`@928, egress sub-block@930, `create_exec_launcher`@932, `async def close()`@1144, egress teardown@1164-1176, imports@35-39.
- mcp.py: `class McpServerConnection`@375, `_open_stdio_transport`@976-1021, `StdioServerParameters(env=...)`@1016, parent (unsandboxed) spawn@979, `self.config: MCPServerConfig`@391. Construction sites: `runner/mcp_manager.py:509`, `server/mcp_pool.py:356`. `MCPServerConfig`@spec/types.py:839.
- pyproject: `asyncio_mode="auto"`@273.

---

## PHASE 0 — Datamodel, spec→policy bridge, parsing

### Task 1: Datamodel dataclasses
**Files:** Modify `omnigent/inner/datamodel.py` (after `CredentialProxySpec`, ~459). Test `tests/inner/test_credential_broker_datamodel.py`.

- [ ] **Step 1: Failing test**
```python
from omnigent.inner.datamodel import (
    CredentialBrokerField, CredentialBrokerLoadSource, CredentialBrokerGroup,
    CredentialBrokerTool, CredentialBrokerSpec, CredentialSourceSpec)

def test_field_defaults():
    f = CredentialBrokerField(env="PGPASSWORD")
    assert f.key is None and f.optional is False and f.fallback is None

def test_spec_composition():
    spec = CredentialBrokerSpec(
        load=[CredentialBrokerLoadSource(from_="env", names=["PGHOST"])],
        groups={"postgres": CredentialBrokerGroup(fields=[
            CredentialBrokerField(env="PGPASSWORD", optional=True,
                fallback=CredentialSourceSpec(kind="command", command="echo x"))])},
        tools={"psql": CredentialBrokerTool(credentials=["postgres"])})
    assert spec.tools["psql"].credentials == ["postgres"]
    assert spec.groups["postgres"].fields[0].fallback.command == "echo x"
```
- [ ] **Step 2: Run, expect ImportError** — `pytest tests/inner/test_credential_broker_datamodel.py -v`
- [ ] **Step 3: Add dataclasses**
```python
@dataclass
class CredentialBrokerField:
    env: str
    key: str | None = None
    optional: bool = False
    fallback: CredentialSourceSpec | None = None

@dataclass
class CredentialBrokerLoadSource:
    from_: Literal["file", "env"]
    path: str | None = None
    names: list[str] = field(default_factory=list)

@dataclass
class CredentialBrokerGroup:
    fields: list[CredentialBrokerField]

@dataclass
class CredentialBrokerTool:
    credentials: list[str]
    binary: str | None = None

@dataclass
class CredentialBrokerSpec:
    load: list[CredentialBrokerLoadSource] = field(default_factory=list)
    groups: dict[str, CredentialBrokerGroup] = field(default_factory=dict)
    tools: dict[str, CredentialBrokerTool] = field(default_factory=dict)
```
- [ ] **Step 4: Add field to `OSEnvSandboxSpec`** (after `credential_proxy`, ~658): `credential_broker: CredentialBrokerSpec | None = None`
- [ ] **Step 5: Run, expect PASS** · **Step 6: Commit** `feat(sandbox): add credential_broker datamodel`

### Task 2: Carry on `SandboxPolicy` (parent-side only)
**Files:** `omnigent/inner/sandbox.py` (field@157, import@21, `_clone_policy_with`@407). Test `tests/inner/test_credential_broker_policy.py`.

- [ ] **Step 1: Failing test**
```python
from pathlib import Path
from omnigent.inner.sandbox import SandboxPolicy, with_additional_write_roots
from omnigent.inner.datamodel import CredentialBrokerSpec

def _policy(**kw):
    return SandboxPolicy(backend_type="linux_bwrap", active=True, read_roots=None,
                         write_roots=[Path("/x")], write_files=[], allow_network=True, **kw)

def test_broker_not_serialized():
    assert "credential_broker" not in _policy(credential_broker=CredentialBrokerSpec()).to_jsonable()

def test_broker_preserved_across_clone():
    spec = CredentialBrokerSpec()
    p = with_additional_write_roots(_policy(credential_broker=spec), [Path("/scratch")])
    assert p.credential_broker is spec
```
- [ ] **Step 2: Run, expect FAIL** (unexpected kwarg)
- [ ] **Step 3:** Add `credential_broker: CredentialBrokerSpec | None = None` after line 157; update import@21 to include `CredentialBrokerSpec`; add `credential_broker=policy.credential_broker,` in `_clone_policy_with`'s `SandboxPolicy(...)` (beside `credential_proxy=`, ~407). Do NOT add to `to_jsonable`/`from_jsonable`.
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `feat(sandbox): carry credential_broker on SandboxPolicy (parent-side only)`

### Task 2b: Spec→policy bridge — **CRITICAL: without this the feature silently no-ops**
**Files:** `omnigent/inner/bwrap_sandbox.py` (resolve ~263-279), `omnigent/inner/seatbelt_sandbox.py` (resolve ~426-441). Test `tests/inner/test_credential_broker_bridge.py`.

- [ ] **Step 1: Failing test**
```python
from pathlib import Path
from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec, CredentialBrokerSpec
from omnigent.inner.sandbox import resolve_sandbox
import pytest, sys

@pytest.mark.parametrize("backend", ["linux_bwrap", "darwin_seatbelt"])
def test_bridge_copies_broker_spec(backend, tmp_path):
    if backend == "linux_bwrap" and sys.platform == "darwin": pytest.skip("bwrap binary absent")
    if backend == "darwin_seatbelt" and not sys.platform == "darwin": pytest.skip("seatbelt is darwin-only")
    spec = OSEnvSpec(type="caller_process", cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type=backend, allow_network=True, credential_broker=CredentialBrokerSpec()))
    policy = resolve_sandbox(spec, tmp_path)
    assert policy.credential_broker is spec.sandbox.credential_broker
```
- [ ] **Step 2: Run, expect FAIL** (`policy.credential_broker is None`)
- [ ] **Step 3:** In BOTH `BwrapSandboxBackend.resolve` (beside `credential_proxy=sandbox_spec.credential_proxy,` @278) and `SeatbeltSandboxBackend.resolve` (@441), add: `credential_broker=sandbox_spec.credential_broker,`
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `fix(sandbox): bridge credential_broker spec→policy in both backends`

### Task 3: Parser — Pydantic boundary models
**Files:** `omnigent/spec/parser.py` (near `_CredentialProxyItemModel` ~1166; wire into `_parse_os_env_sandbox` ~826 + constructor ~852). Test `tests/spec/test_parse_credential_broker.py`.

- [ ] **Step 1: Failing test**
```python
import pytest
from omnigent.spec.parser import _parse_os_env_sandbox
from omnigent.errors import OmnigentError  # adjust import to the repo's error module

def test_parse_broker():
    raw = {"type": "linux_bwrap", "allow_network": True, "credential_broker": {
        "load": [{"from": "env", "names": ["PGHOST"]}],
        "groups": {"postgres": [{"env": "PGPASSWORD", "optional": True,
                                 "fallback": {"kind": "command", "command": "az ... -o tsv"}},
                                {"env": "PGHOST"}]},
        "tools": {"psql": {"credentials": ["postgres"]}}}}
    spec = _parse_os_env_sandbox(raw)
    b = spec.credential_broker
    assert b.load[0].from_ == "env" and b.load[0].names == ["PGHOST"]
    assert b.groups["postgres"].fields[0].fallback.command == "az ... -o tsv"
    assert b.tools["psql"].credentials == ["postgres"]

def test_parse_broker_unknown_group_raises():
    raw = {"type": "linux_bwrap", "allow_network": True,
           "credential_broker": {"groups": {}, "tools": {"psql": {"credentials": ["nope"]}}}}
    with pytest.raises(OmnigentError): _parse_os_env_sandbox(raw)

def test_parse_broker_interpreter_hook_env_raises():
    raw = {"type": "linux_bwrap", "allow_network": True,
           "credential_broker": {"groups": {"g": [{"env": "LD_PRELOAD"}]}, "tools": {"psql": {"credentials": ["g"]}}}}
    with pytest.raises(OmnigentError): _parse_os_env_sandbox(raw)
```
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement boundary models**
```python
_FIELD_ENV_DENYLIST = frozenset({
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
    "BASH_ENV", "ENV", "PATH", "PYTHONPATH", "PYTHONSTARTUP", "IFS"})

class _CredentialBrokerFieldModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    env: str
    key: str | None = None
    optional: bool = False
    fallback: _CredentialSourceModel | None = None
    def to_spec(self) -> CredentialBrokerField:
        return CredentialBrokerField(env=self.env, key=self.key, optional=self.optional,
            fallback=self.fallback.to_spec() if self.fallback else None)

class _CredentialBrokerLoadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    from_: Literal["file", "env"] = Field(alias="from")
    path: str | None = None
    names: list[str] = Field(default_factory=list)
    def to_spec(self) -> CredentialBrokerLoadSource:
        return CredentialBrokerLoadSource(from_=self.from_, path=self.path, names=self.names)

class _CredentialBrokerToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credentials: list[str]
    binary: str | None = None

class _CredentialBrokerModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    load: list[_CredentialBrokerLoadModel] = Field(default_factory=list)
    groups: dict[str, list[_CredentialBrokerFieldModel]] = Field(default_factory=dict)
    tools: dict[str, _CredentialBrokerToolModel] = Field(default_factory=dict)
    @model_validator(mode="after")
    def _check(self):
        for gname, fields in self.groups.items():
            for f in fields:
                if f.env in _FIELD_ENV_DENYLIST:
                    raise ValueError(f"credential_broker group {gname!r} may not set interpreter-hook env {f.env!r}")
        for tname, t in self.tools.items():
            for c in t.credentials:
                if c not in self.groups:
                    raise ValueError(f"credential_broker tool {tname!r} references unknown group {c!r}")
        return self

def _parse_credential_broker(raw):
    if raw is None: return None
    try:
        m = _CredentialBrokerModel.model_validate(raw)
    except ValidationError as exc:
        raise OmnigentError(code=ErrorCode.INVALID_INPUT, message=f"invalid credential_broker: {exc}") from exc
    return CredentialBrokerSpec(
        load=[l.to_spec() for l in m.load],
        groups={g: CredentialBrokerGroup(fields=[f.to_spec() for f in fs]) for g, fs in m.groups.items()},
        tools={t: CredentialBrokerTool(credentials=v.credentials, binary=v.binary) for t, v in m.tools.items()})
```
Wire: `credential_broker = _parse_credential_broker(raw.get("credential_broker"))` in `_parse_os_env_sandbox`; add `credential_broker=credential_broker,` to the `OSEnvSandboxSpec(...)` constructor @852. Use the repo's actual `OmnigentError`/`ErrorCode` import.
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `feat(spec): parse credential_broker (pydantic boundary models)`

### Task 3b: Cross-field validation — inline in BOTH paths (native error types)
**Files:** `omnigent/spec/parser.py` (`_parse_os_env_sandbox`, raise `OmnigentError`) and `omnigent/inner/loader.py` (`_parse_os_env_sandbox_spec`, raise `ValueError`).

- [ ] **Step 1: Failing tests** (one per path)
```python
import pytest
from omnigent.spec.parser import _parse_os_env_sandbox
def _broker(): return {"groups": {"g": [{"env": "PGHOST"}]}, "tools": {"psql": {"credentials": ["g"]}}}
def test_broker_rejects_egress():
    with pytest.raises(Exception, match="not compatible with egress_rules"):
        _parse_os_env_sandbox({"type": "linux_bwrap", "allow_network": True, "egress_rules": ["GET x/**"], "credential_broker": _broker()})
def test_broker_rejects_no_network():
    with pytest.raises(Exception, match="requires allow_network"):
        _parse_os_env_sandbox({"type": "linux_bwrap", "allow_network": False, "credential_broker": _broker()})
def test_broker_rejects_type_none():
    with pytest.raises(Exception, match="requires sandbox type"):
        _parse_os_env_sandbox({"type": "none", "credential_broker": _broker()})
```
- [ ] **Step 2: Run FAIL**
- [ ] **Step 3:** After building the `OSEnvSandboxSpec`, in the spec parser add (raising `OmnigentError`) and mirror in the loader (raising `ValueError`):
```python
    if spec.credential_broker is not None:
        if spec.egress_rules:
            raise <Error>("credential_broker is not compatible with egress_rules: brokered tools need raw TCP; egress isolates the network to an HTTP-only proxy.")
        if spec.allow_network is False:
            raise <Error>("credential_broker requires allow_network: true.")
        if spec.type not in ("linux_bwrap", "darwin_seatbelt"):
            raise <Error>("credential_broker requires sandbox type linux_bwrap or darwin_seatbelt.")
```
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `feat(spec): validate credential_broker (network/egress/backend)`

### Task 4: Loader reuse — `omnigent/inner/loader.py`
- [ ] **Step 1: Failing test** — `load_agent_def` roundtrips a broker spec (assert `agent.os_env.sandbox.credential_broker.tools["psql"].credentials == ["pg"]`).
- [ ] **Step 2: Run FAIL**
- [ ] **Step 3:** Import `_parse_credential_broker` from `omnigent.spec.parser` (pattern @787); `credential_broker = _parse_credential_broker(data.get("credential_broker"))`; add `credential_broker=credential_broker,` to the `OSEnvSandboxSpec(...)` constructor @816; add the Task-3b checks (raising `ValueError`).
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `feat(loader): credential_broker via shared parser + validation`

---

## PHASE 1 — Broker core + os_env shell (secure v1)

New module `omnigent/inner/credential_broker.py` (Tasks 5–8, 10) + `omnigent/inner/cred_broker_client.py` (Task 9). Tests in `tests/inner/test_credential_broker.py` + `tests/inner/test_cred_broker_client.py` + `tests/inner/test_os_env_broker_e2e.py`.

Module header:
```python
"""Parent-side broker for non-HTTP credentialed tools. Real secrets resolve in
the parent (loaded at session start, or per-call via a fallback) and reach a
single tool invocation over an AF_UNIX socket in the helper's bound scratch dir,
authenticated by a per-handle token. Values are never logged."""
from __future__ import annotations
import contextlib, hmac, json, logging, os, secrets, shutil, socket, struct, sys, threading
from dataclasses import dataclass
from pathlib import Path
from .credential_proxy import _resolve_secret  # reuse env/file/command resolver (private; same package)
from .datamodel import CredentialBrokerSpec, CredentialBrokerLoadSource
logger = logging.getLogger(__name__)
```

### Task 5: `_load_store`
- [ ] **Test** (file KEY=VALUE + env-name lift, perms warning) → **implement**:
```python
def _load_store(load_sources, *, parent_env):
    store = {}
    for src in load_sources:
        if src.from_ == "file":
            if not src.path: raise ValueError("credential_broker load file source requires 'path'")
            p = Path(os.path.expanduser(src.path))
            if not p.is_file(): raise ValueError(f"credential_broker load file not found: {p}")
            if p.stat().st_mode & 0o077: logger.warning("credential_broker load file %s is group/other-accessible (want 0600)", p)
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1); store[k.strip()] = v.strip()
        elif src.from_ == "env":
            for name in src.names:
                val = parent_env.get(name)
                if val is not None: store[name] = val
    return store
```
- [ ] **Commit** `feat(broker): load-at-unlock store`

### Task 6: `_resolve_tool_env` (filtered command env)
- [ ] **Test** (store→fallback→optional-skip; required-missing raises generic) → **implement**:
```python
def _resolve_tool_env(spec, tool_name, store, *, command_env):
    tool = spec.tools[tool_name]; out = {}
    for gname in tool.credentials:
        for f in spec.groups[gname].fields:
            val = store.get(f.key or f.env)
            if val is None and f.fallback is not None:
                try: val = _resolve_secret(f.fallback, parent_env=command_env)
                except ValueError: val = None
            if val is None:
                if f.optional: continue
                raise ValueError(f"required field {f.env} for tool {tool_name!r} unresolved")
            out[f.env] = val
    return out
```
- [ ] **Commit** `feat(broker): per-tool credential resolution`

### Task 7: `_write_shims` (self-contained client)
- [ ] **Test** (shim + copied client exist, executable; shim references socket+tool; no `omnigent` import in copied client) → **implement**:
```python
def _write_shims(tool_names, shim_dir: Path, *, socket_path: Path) -> Path:
    shim_dir.mkdir(parents=True, exist_ok=True)
    import omnigent.inner.cred_broker_client as _client_mod
    client_dst = shim_dir / "cred_broker_client.py"
    client_dst.write_text(Path(_client_mod.__file__).read_text(encoding="utf-8"), encoding="utf-8")
    py = sys.executable
    for name in tool_names:
        shim = shim_dir / name
        shim.write_text("#!/bin/bash\n"
            f'exec "{py}" "{client_dst}" --socket "{socket_path}" --tool "{name}" -- "$@"\n')
        shim.chmod(0o755)
    return shim_dir
```
- [ ] **Commit** `feat(broker): self-contained PATH shim writer`

### Task 8: `_BrokerServer` (token, umask, value-free errors)
- [ ] **Test** (token required; bad token denied; unknown tool denied; uid mismatch denied; error responses contain no secret values) → **implement**:
```python
def _recv_line(conn):
    buf = b""
    while not buf.endswith(b"\n"):
        ch = conn.recv(65536)
        if not ch: break
        buf += ch
    return buf.decode("utf-8")

def _peer_uid(conn):
    if sys.platform.startswith("linux"):
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw); return uid
    if sys.platform == "darwin":
        SOL_LOCAL, LOCAL_PEERCRED = 0, 0x001
        try:
            raw = conn.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, 8)
            _version, uid = struct.unpack("II", raw[:8]); return uid
        except OSError:
            return None
    return None

class _BrokerServer:
    def __init__(self, *, spec, store, parent_env, command_env, socket_path, auth_token):
        self._spec, self._store = spec, store
        self._parent_env, self._command_env = parent_env, command_env
        self.socket_path = socket_path; self._auth_token = auth_token
        self._sock = None; self._thread = None; self._running = False
    def start(self):
        old = os.umask(0o077)
        try:
            if self.socket_path.exists(): self.socket_path.unlink()
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.bind(str(self.socket_path))
        finally:
            os.umask(old)
        os.chmod(self.socket_path, 0o600); s.listen(8); s.settimeout(0.5)
        self._sock, self._running = s, True
        self._thread = threading.Thread(target=self._accept_loop, daemon=True); self._thread.start()
    def _accept_loop(self):
        while self._running:
            try: conn, _ = self._sock.accept()
            except socket.timeout: continue
            except OSError: break
            with conn: self._handle(conn)
    def _handle(self, conn):
        try:
            if _peer_uid(conn) not in (None, os.getuid()):
                conn.sendall(b'{"error":"denied"}\n'); return
            req = json.loads(_recv_line(conn))
            if not hmac.compare_digest(str(req.get("token", "")), self._auth_token):
                logger.warning("credential_broker: bad token"); conn.sendall(b'{"error":"denied"}\n'); return
            tool = req.get("tool")
            if tool not in self._spec.tools:
                conn.sendall(b'{"error":"unknown tool"}\n'); return
            env = _resolve_tool_env(self._spec, tool, self._store, command_env=self._command_env)
            binary = self._spec.tools[tool].binary or shutil.which(tool, path=self._parent_env.get("PATH", os.defpath)) or tool
            logger.info("credential_broker: served tool=%s keys=%s", tool, sorted(env))
            conn.sendall((json.dumps({"env": env, "binary": binary}) + "\n").encode("utf-8"))
        except Exception as exc:
            logger.warning("credential_broker: resolution failed: %s", exc)
            conn.sendall(b'{"error":"resolution failed"}\n')
    def stop(self):
        self._running = False
        if self._sock is not None:
            with contextlib.suppress(OSError): self._sock.close()
        if self._thread is not None: self._thread.join(timeout=2)
        with contextlib.suppress(OSError): self.socket_path.unlink()
```
**VERIFY on macOS:** `_peer_uid` xucred layout (falls back to `None` → uid check skipped, socket perms still gate).
- [ ] **Commit** `feat(broker): AF_UNIX server with per-handle token`

### Task 9: `omnigent/inner/cred_broker_client.py` (self-contained, stdlib only)
- [ ] **Test** (fake server → env injected → execv fake tool echoes cred; reads token from `OMNIGENT_CRED_BROKER_TOKEN`) → **implement**:
```python
"""Trusted client the PATH shim execs INSIDE the sandbox. MUST NOT import omnigent."""
import json, os, socket, sys
def _request(socket_path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(socket_path); s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            ch = s.recv(65536)
            if not ch: break
            buf += ch
    resp = json.loads(buf.decode())
    if "error" in resp:
        print(f"cred-broker: {resp['error']}", file=sys.stderr); raise SystemExit(2)
    return resp
def main(argv=None):
    a = argv if argv is not None else sys.argv[1:]
    sock = a[a.index("--socket") + 1]; tool = a[a.index("--tool") + 1]; rest = a[a.index("--") + 1:]
    token = os.environ.get("OMNIGENT_CRED_BROKER_TOKEN", "")
    resp = _request(sock, {"tool": tool, "token": token})
    os.environ.update(resp["env"]); b = resp["binary"]; os.execv(b, [b, *rest])
if __name__ == "__main__":
    raise SystemExit(main())
```
- [ ] **Commit** `feat(broker): self-contained in-sandbox client`

### Task 10: `prepare_credential_broker_runtime`
- [ ] **Test** (runtime builds shim+client+socket; `.stop()` removes socket + shim dir; carries `auth_token`) → **implement**:
```python
@dataclass
class CredentialBrokerRuntime:
    shim_dir: Path; socket_path: Path; auth_token: str; _server: _BrokerServer
    def stop(self):
        self._server.stop(); shutil.rmtree(self.shim_dir, ignore_errors=True)

def prepare_credential_broker_runtime(spec, *, parent_env, command_env, scratch_dir):
    if spec is None: return None
    token = secrets.token_urlsafe(32)
    store = _load_store(spec.load, parent_env=parent_env)
    server = _BrokerServer(spec=spec, store=store, parent_env=parent_env,
                           command_env=command_env, socket_path=scratch_dir / "cred-broker.sock", auth_token=token)
    server.start()
    shim_dir = _write_shims(list(spec.tools), scratch_dir / "cred-shims", socket_path=server.socket_path)
    return CredentialBrokerRuntime(shim_dir=shim_dir, socket_path=server.socket_path, auth_token=token, _server=server)
```
Export the public names in `__all__`.
- [ ] **Commit** `feat(broker): runtime assembly`

### Task 11: Wire into os_env helper + token plumb
**Files:** `omnigent/inner/os_env.py` (`__init__`@339, `_start_locked` after credential_proxy block ~434, config dict ~449, `_run_helper` ~1427-1442, `_stop_locked` ~584).

- [ ] **Step 1: Failing E2E test** (`tests/inner/test_os_env_broker_e2e.py`)
```python
import os, sys, shutil, pytest
from omnigent.inner.os_env import create_os_environment
from omnigent.inner.datamodel import (OSEnvSpec, OSEnvSandboxSpec, CredentialBrokerSpec,
    CredentialBrokerGroup, CredentialBrokerField, CredentialBrokerTool, CredentialBrokerLoadSource)

pytestmark = pytest.mark.skipif(not (sys.platform == "darwin" or shutil.which("bwrap")),
                                reason="needs an active sandbox backend")

def _spec(work, env_file, backend):
    return OSEnvSpec(type="caller_process", cwd=str(work), sandbox=OSEnvSandboxSpec(
        type=backend, allow_network=True, write_paths=["."], read_paths=[str(work)],
        credential_broker=CredentialBrokerSpec(
            load=[CredentialBrokerLoadSource(from_="file", path=str(env_file))],
            groups={"pg": CredentialBrokerGroup(fields=[CredentialBrokerField(env="PGPASSWORD")])},
            tools={"faketool": CredentialBrokerTool(credentials=["pg"], binary=None)})))

async def _run(work, env_file, backend, fake):
    spec = _spec(work, env_file, backend)
    spec.sandbox.credential_broker.tools["faketool"].binary = str(fake)
    env = create_os_environment(spec)
    try:
        via = await env.shell("faketool")
        leak = await env.shell('printf "%s" "${PGPASSWORD:-EMPTY}"')
        return via["stdout"], leak["stdout"].strip()
    finally:
        env.close()

@pytest.mark.asyncio
async def test_cred_reaches_tool_not_agent(tmp_path):
    backend = "darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap"
    (tmp_path / "dev.env").write_text("PGPASSWORD=s3cret\n"); (tmp_path / "dev.env").chmod(0o600)
    work = tmp_path / "work"; work.mkdir()
    fake = work / "faketool"; fake.write_text('#!/bin/bash\necho "PG=$PGPASSWORD"\n'); fake.chmod(0o755)
    out, leak = await _run(work, tmp_path / "dev.env", backend, fake)
    assert "PG=s3cret" in out          # reached the tool via shim
    assert leak == "EMPTY"             # NOT in the agent's ambient env

@pytest.mark.asyncio
async def test_cred_reaches_tool_cwd_outside_repo(tmp_path):
    # FIX M6 regression guard: shim works when cwd is not the omnigent repo.
    backend = "darwin_seatbelt" if sys.platform == "darwin" else "linux_bwrap"
    (tmp_path / "dev.env").write_text("PGPASSWORD=s3cret\n"); (tmp_path / "dev.env").chmod(0o600)
    work = tmp_path / "elsewhere"; work.mkdir()
    fake = work / "faketool"; fake.write_text('#!/bin/bash\necho "PG=$PGPASSWORD"\n'); fake.chmod(0o755)
    out, _ = await _run(work, tmp_path / "dev.env", backend, fake)
    assert "PG=s3cret" in out
```
- [ ] **Step 2: Run FAIL**
- [ ] **Step 3a:** `__init__`@339: add `self._broker_runtime = None`.
- [ ] **Step 3b:** `_start_locked`, after the credential_proxy block (~434, inside `if sandbox.active:`):
```python
            if sandbox.credential_broker is not None:
                if self._broker_runtime is not None:
                    self._broker_runtime.stop(); self._broker_runtime = None
                from .credential_broker import prepare_credential_broker_runtime
                self._broker_runtime = prepare_credential_broker_runtime(
                    sandbox.credential_broker, parent_env=dict(os.environ),
                    command_env=dict(env), scratch_dir=self._tmpdir)
                if self._broker_runtime is not None:
                    env["PATH"] = f"{self._broker_runtime.shim_dir}{os.pathsep}{env['PATH']}"
```
- [ ] **Step 3c:** config dict (~449), beside the egress token: `if self._broker_runtime is not None: config["cred_broker_token"] = self._broker_runtime.auth_token`
- [ ] **Step 3d:** `_run_helper` (beside the egress token splice ~1427): `tok = config.get("cred_broker_token");` if str+truthy: `os.environ["OMNIGENT_CRED_BROKER_TOKEN"] = tok` (in-process; not in execve snapshot).
- [ ] **Step 3e:** `_stop_locked` (after `_stop_egress_proxy_locked()` ~584): `if self._broker_runtime is not None: self._broker_runtime.stop(); self._broker_runtime = None`.
- [ ] **Step 4: Run PASS** (both E2E variants) · **Step 5: Commit** `feat(os_env): credential broker on PATH for sandboxed shell tools`

---

## PHASE 2 — Native harness terminals (H1 caveat — see below)

### Task 12: Shim on PATH in `terminal.py`
**Files:** `omnigent/inner/terminal.py` (dataclass fields ~781, `launch()` ~928, `close()` ~1144). Test `tests/inner/test_terminal_broker.py`.

- [ ] **Step 1: Failing test** — build a `Terminal` via the existing path with a `credential_broker` sandbox; assert the launch env `PATH[0]` is the shim dir and `PGPASSWORD` is absent from the env. (Mirror existing terminal-test construction.)
- [ ] **Step 2: Run FAIL**
- [ ] **Step 3a:** Declare beside lines 781-782: `_broker_runtime: CredentialBrokerRuntime | None = field(default=None, repr=False)` and `_broker_dir: Path | None = field(default=None, repr=False)`. Import `prepare_credential_broker_runtime`/`CredentialBrokerRuntime`, `build_helper_env`, `with_additional_write_roots`, `create_private_tmpdir`, `cleanup_private_tmpdir`.
- [ ] **Step 3b:** Inside the existing `if sandbox_for_launcher is not None and sandbox_for_launcher.active:` block (@928), after the egress sub-block (@930), BEFORE `create_exec_launcher` (@932):
```python
            if sandbox_for_launcher.credential_broker is not None:
                self._broker_dir = create_private_tmpdir()
                command_env = build_helper_env(os.environ, sandbox_for_launcher)  # filtered, NOT dict(env)
                self._broker_runtime = prepare_credential_broker_runtime(
                    sandbox_for_launcher.credential_broker, parent_env=dict(os.environ),
                    command_env=command_env, scratch_dir=self._broker_dir)
                if self._broker_runtime is not None:
                    sandbox_for_launcher = with_additional_write_roots(sandbox_for_launcher, [self._broker_dir])
                    env["PATH"] = f"{self._broker_runtime.shim_dir}{os.pathsep}{env['PATH']}"
                    env["OMNIGENT_CRED_BROKER_TOKEN"] = self._broker_runtime.auth_token  # see CAVEAT
```
- [ ] **Step 3c:** Teardown in `async def close()` (@1144, mirror egress @1164-1176): `if self._broker_runtime is not None: self._broker_runtime.stop(); self._broker_runtime = None` and `cleanup_private_tmpdir(self._broker_dir); self._broker_dir = None`.
- [ ] **Step 4: Run PASS** · **Step 5: Commit** `feat(terminal): credential broker on PATH for native harnesses`

> **OPEN CAVEAT (H1, terminals):** setting `OMNIGENT_CRED_BROKER_TOKEN` in the terminal `env` lands it in the harness's execve `/proc/<pid>/environ` snapshot (readable by other same-uid processes), unlike os_env which injects it in-process. **Resolve before claiming H1-closed for terminals:** read how the terminal egress path delivers its auth material (`terminal.py` egress sub-block @930 + `run_launcher` @703) and mirror an out-of-band channel; if none exists, gate broker-on-terminals to single-agent-per-uid deployments or defer this Phase. Add a follow-up task to either (a) inject the token in-process inside `run_launcher` before `subprocess.run`, accepting it still lands in the harness env, or (b) deliver via a 0600 file the launcher reads and unlinks. Document the residual until resolved.

---

## PHASE 3 — MCP server launches (parent-side; security-simple; needs threading)

MCP servers spawn **unsandboxed in the parent** (`mcp.py:979`) → no socket, no token, no in-sandbox exposure. The only work is plumbing the broker spec to the MCP layer, which currently has no reference to it.

### Task 13a: Config field
- [ ] Add `credential_groups: list[str] = field(default_factory=list)` to `MCPServerConfig` (`spec/types.py:839`) + its parser. Test: parses; defaults empty.

### Task 13b: Thread the broker spec to the MCP connection
- [ ] **Map and implement.** `McpServerConnection` is built at `runner/mcp_manager.py:509` and `server/mcp_pool.py:356` (`McpServerConnection(config=server.config)`); `tools/mcp.py` has no access to `os_env.sandbox.credential_broker`. The broker spec is reachable from the agent via `agent.os_env.sandbox.credential_broker` (cf. `loader.py:_effective_terminal_sandbox` @689-703). Pass the `CredentialBrokerSpec` (or a pre-resolved `_load_store` result) into `McpServerConnection.__init__` from `mcp_manager.py:509` (runner has agent context). **Risk to verify first:** `server/mcp_pool.py:356` is a server-side pool that may be agent-agnostic — confirm it can see the owning agent's os_env spec; if not, broker-for-MCP applies only to the runner path and the pool path must be left unbrokered (document) or fed via the pool's per-agent config. Write this as the first sub-step (a grep + read of both construction sites + their callers), then implement.
- [ ] Test: connection receives the spec; absent → None.

### Task 13c: Merge resolved creds into spawn env
- [ ] At `_open_stdio_transport` (~1016): if `self.config.credential_groups` and a broker spec is present, build a synthetic one-tool `CredentialBrokerSpec` over those groups, resolve via `_load_store` + `_resolve_tool_env(command_env=<filtered or strip_runner_auth_secrets(os.environ)>)`, and merge into `env`:
```python
        broker_env = {}
        if self._broker_spec is not None and self.config.credential_groups:
            from omnigent.inner.credential_broker import _load_store, _resolve_tool_env
            from omnigent.inner.datamodel import CredentialBrokerSpec, CredentialBrokerTool
            synthetic = CredentialBrokerSpec(load=self._broker_spec.load, groups=self._broker_spec.groups,
                tools={"__mcp__": CredentialBrokerTool(credentials=list(self.config.credential_groups))})
            store = _load_store(synthetic.load, parent_env=dict(os.environ))
            broker_env = _resolve_tool_env(synthetic, "__mcp__", store, command_env=dict(os.environ))
        env = strip_runner_auth_secrets(os.environ) | self.config.env | broker_env
```
- [ ] Test: MCP env carries resolved creds when groups set; unchanged otherwise. · **Commit** `feat(mcp): resolve broker credential groups into server env`

---

## Hygiene & docs
- [ ] **L1:** in `prepare_credential_broker_runtime` (or os_env wiring), assert the broker socket path is not in `policy.deny_unix_socket_paths`; raise a clear config error if it is.
- [ ] **Docs:** add a `docs/` section: schema, the two-tier guarantee, `allow_network: true` requirement, `egress_rules` incompatibility, and the tool-binary reachability contract (system `/usr/bin` bound by default; for `/opt/homebrew`/`/usr/local`/venv set `binary:` AND add its dir to `sandbox.read_paths`, on seatbelt too).

## Example YAML
```yaml
sandbox:
  type: linux_bwrap
  allow_network: true
  credential_broker:
    load:
      - { from: env, names: [PGHOST, PGPORT, PGDATABASE] }   # non-secret config
    groups:
      postgres:
        fields:
          - { env: PGPASSWORD, optional: true, fallback: {kind: command, command: "az account get-access-token --resource https://ossrdbms-aad.database.windows.net --query accessToken -o tsv"} }  # ephemeral: never in sandbox
          - { env: PGUSER, optional: true, fallback: {kind: command, command: "az account show --query user.name -o tsv"} }
          - { env: PGHOST }
          - { env: PGPORT }
          - { env: PGDATABASE }
    tools:
      psql:   { credentials: [postgres] }
      pytest: { credentials: [postgres] }
```

## Self-review checklist (run before first commit)
- Spec coverage: load-at-unlock (T5), ephemeral fallback (T6), shell surface (T11), terminal (T12), MCP (T13a-c), parent-side-only/no-serialize (T2), spec→policy bridge (T2b), validation (T3b). ✓
- Type consistency: `from_`, `CredentialBrokerRuntime{shim_dir, socket_path, auth_token}`, `_resolve_tool_env(spec, tool, store, *, command_env)`, `prepare_credential_broker_runtime(spec, *, parent_env, command_env, scratch_dir)`. ✓
- Verify-spots (real steps, not placeholders): macOS `_peer_uid` xucred (T8); terminal H1 token channel (T12 caveat); MCP construction-path mapping (T13b first sub-step).
