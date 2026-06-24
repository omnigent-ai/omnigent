"""Core data model types for Omnigent, which are all exposed to agents and in configs."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, TypeAlias

# ---------------------------------------------------------------------------
# Type aliases for JSON-shaped / heterogeneous-value boundaries
# ---------------------------------------------------------------------------

# ``Message.content`` / metadata values are heterogeneous JSON (str payloads,
# tool-call dicts, provider-specific structured blocks). The inner values are
# opaque at this layer; callers isinstance-narrow or json-serialise as needed.
MessageContent: TypeAlias = str | dict[str, Any]  # type: ignore[explicit-any]
MessageMetadata: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ``AskRequest.content`` and ``LabelSchemaRule.normalize`` accept arbitrary
# JSON-shaped values threaded through the policy/label machinery unchanged.
AskContent: TypeAlias = Any  # type: ignore[explicit-any]
LabelValue: TypeAlias = Any  # type: ignore[explicit-any]

# ``ParamDef.default`` is an arbitrary YAML/JSON value (string, int, list,
# dict, etc.) read verbatim from agent specs.
ParamDefault: TypeAlias = Any  # type: ignore[explicit-any]

# ``AgentDef`` holds registries of Tool / Policy / skill / metadata objects
# whose concrete types live in other modules (omnigent.tools,
# omnigent.policies, ...). Importing them here would create import cycles,
# so we keep these as opaque JSON-shaped dicts at this layer.
ToolRegistry: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
PolicyRegistry: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
SkillRegistry: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]
AgentMetadata: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# ``AgentDef.workflow`` can be a DAG-expression string or a Callable;
# concrete callable shape varies per workflow engine, so it's opaque here.
Workflow: TypeAlias = Any  # type: ignore[explicit-any]


# ---------------------------------------------------------------------------
# Message
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """A single message in a conversation history.

    :param role: Which party authored the message.
    :param content: Message body — plain text or provider-specific
        structured JSON (tool-call/tool-result blocks).
    :param timestamp: UTC creation time; defaults to now.
    :param source: Which connection or tool the message originated
        from, e.g. ``"primary"`` or a tool name. ``None`` when the
        message was not attributed to a specific source.
    :param metadata: Arbitrary JSON-shaped metadata attached by the
        runtime (tracing, policy annotations, etc.).
    """

    role: Literal["system", "user", "assistant", "tool_call", "tool_result"]
    content: MessageContent
    timestamp: datetime = field(default_factory=datetime.utcnow)
    source: str | None = None
    metadata: MessageMetadata = field(default_factory=dict)


@dataclass
class AskRequest:
    """Structured approval request for policy ASK decisions.

    :param reason: Human-readable explanation of why approval is
        being requested.
    :param phase: Lifecycle phase the decision is gating, e.g.
        ``"pre_tool"`` or ``"pre_response"``.
    :param content: Arbitrary JSON-shaped payload the policy wants
        reviewed (tool args, draft response text, etc.).
    :param session_id: Stable identifier of the requesting session,
        e.g. ``"9d3b..."``. ``None`` when the request is not
        associated with a persisted session.
    :param session_label: Human-readable label for the requesting
        session, e.g. the agent name. ``None`` when unset.
    """

    reason: str
    phase: str
    content: AskContent = None
    session_id: str | None = None
    session_label: str | None = None


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


class History:
    """Ordered record of all messages in a session or passed from a parent."""

    def __init__(self, name: str = "self") -> None:
        self.name = name
        self.messages: list[Message] = []

    def append(self, msg: Message) -> None:
        self.messages.append(msg)

    def get_context_window(self, max_tokens: int | None = None) -> list[Message]:  # noqa: ARG002 — placeholder API; token-based trimming not yet implemented
        """
        Return messages that fit in the context window.

        For now, return all messages.  A real implementation would count tokens
        and summarise older messages.
        """
        return list(self.messages)

    def search(self, query: str) -> list[Message]:
        """Simple substring search over message content."""
        results: list[Message] = []
        for msg in self.messages:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            if query.lower() in text.lower():
                results.append(msg)
        return results

    def as_text(self) -> str:
        parts: list[str] = []
        for msg in self.messages:
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            parts.append(f"[{msg.role}] {text}")
        return "\n".join(parts)

    def __len__(self) -> int:
        return len(self.messages)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class Connection:
    """A bidirectional message channel.

    In a real deployment, this wraps a WebSocket, Slack channel, REST request,
    etc.  For the prototype, we use simple async queues.
    """

    def __init__(self, name: str = "primary", conn_type: str = "chat") -> None:
        import asyncio

        self.id: str = str(uuid.uuid4())
        self.name = name
        self.type = conn_type
        self._inbox: asyncio.Queue[Message] = asyncio.Queue()
        self._outbox: asyncio.Queue[str] = asyncio.Queue()

    async def receive(self) -> Message:
        return await self._inbox.get()

    def try_receive_nowait(self) -> Message | None:
        import asyncio

        try:
            return self._inbox.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def send(self, content: str) -> None:
        await self._outbox.put(content)

    # Helpers for tests / programmatic usage
    async def inject_user_message(self, text: str) -> None:
        """Simulate a user sending a message on this connection."""
        msg = Message(role="user", content=text, source=self.name)
        await self._inbox.put(msg)

    async def read_agent_response(self) -> str:
        """Read the next agent response from the outbox."""
        return await self._outbox.get()


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


@dataclass
class MemoryConfig:
    """Configuration for a named memory store."""

    scope: Literal["per_session", "per_user", "cross_user"] = "per_session"


class Memory:
    """A simple in-memory key-value store.

    In production, this would be backed by Postgres.
    """

    def __init__(self, name: str, scope: str = "per_session") -> None:
        self.name = name
        self.scope = scope
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    def peek(self, key: str) -> str | None:
        """
        Synchronous snapshot read of a memory value.

        ``Runtime.execute`` runs plain sync ``exec()``, so ``get()``
        (which is async) would return a bare coroutine from inside
        runtime code. ``peek`` is the sync read path runtime code
        should use.
        """
        return self._store.get(key)

    async def set(self, key: str, value: str) -> None:
        self._store[key] = value

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        """
        Return keys in this memory store, optionally filtered by prefix.

        :param prefix: Only return keys starting with this prefix.
            ``None`` (the default) returns all keys.
        """
        if prefix is None:
            return list(self._store.keys())
        return [k for k in self._store if k.startswith(prefix)]

    async def search(self, query: str, limit: int = 10) -> list[tuple[str, str]]:
        """Simple substring search over values."""
        results: list[tuple[str, str]] = []
        for k, v in self._store.items():
            if query.lower() in v.lower():
                results.append((k, v))
                if len(results) >= limit:
                    break
        return results


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


@dataclass
class Credentials:
    """Credentials carried by a session.  Tools receive these automatically.

    :param token: Bearer token used for downstream API calls, e.g.
        a Databricks PAT. ``None`` when the session is unauthenticated.
    :param scopes: Set of opaque capability scopes the token is
        authorised for, e.g. ``{"sql:read", "files:write"}``.
    :param principal: Identifier of the authenticated user, e.g.
        ``"alice@example.com"``. ``None`` when unknown.
    :param expires_at: UTC expiry; ``None`` if the token does not
        expire or the expiry is unknown.
    """

    token: str | None = None
    scopes: set[str] = field(default_factory=set)
    principal: str | None = None
    expires_at: datetime | None = None

    def attenuate(self, scopes: set[str]) -> Credentials:
        """
        Return a new :class:`Credentials` restricted to a subset of
        scopes.

        :param scopes: Desired scopes; must be a subset of
            ``self.scopes``.
        :raises ValueError: If any requested scope is not already
            held.
        """
        if not scopes.issubset(self.scopes):
            raise ValueError(f"Cannot attenuate: {scopes - self.scopes} not in current scopes")
        return Credentials(
            token=self.token,
            scopes=scopes,
            principal=self.principal,
            expires_at=self.expires_at,
        )


# ---------------------------------------------------------------------------
# ParamDef
# ---------------------------------------------------------------------------


@dataclass
class ParamDef:
    """Describes a parameter an agent requires at instantiation.

    :param type: JSON-Schema-style type tag, e.g. ``"string"`` or
        ``"integer"``. Defaults to ``"string"``.
    :param description: Human-readable description shown to users
        prompting for the parameter. ``None`` when unset.
    :param default: Default value to use when the caller does not
        provide one; ``None`` if the parameter is required.
    """

    type: str = "string"
    description: str | None = None
    default: ParamDefault = None


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class SessionState(enum.Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SLEEPING = "SLEEPING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# ExecutorSpec (lightweight)
# ---------------------------------------------------------------------------


@dataclass
class ExecutorSpec:
    """Which executor/model to use.

    :param model: Model identifier, e.g. ``"databricks-gpt-5-4-mini"``
        or ``"openai/gpt-4o"``. ``None`` when not specified (a
        harness-specific default is used).
    :param harness: Executor harness selector, e.g. ``"claude-sdk"``,
        ``"open-responses"``, ``"codex"``. ``None`` when the default
        harness for the model applies.
    :param profile: Credentials profile name (typically a
        ``~/.databrickscfg`` profile), e.g. ``"<your-profile>"``.
        ``None`` when no profile override is needed.
    :param auth: Parsed auth block from the YAML (e.g. api_key +
        base_url). Carried through so the omnigent spec translator
        can forward it into the child :class:`ExecutorSpec` without
        re-reading raw YAML.
    """

    model: str | None = None
    harness: str | None = None
    profile: str | None = None
    auth: object | None = None  # ApiKeyAuth | DatabricksAuth | None


# ---------------------------------------------------------------------------
# OS environment
# ---------------------------------------------------------------------------

# Basic-auth username GitHub (and ``gh``) accept for token auth: the
# token lives in the password field, so this placeholder username works
# for any GitHub PAT / gh token. Shared by the spec parser (default for
# ``https_basic`` / ``git_https`` / ``gh_basic``), the runtime, and the
# egress proxy's Basic emit path so the literal lives in exactly one
# place.
DEFAULT_BASIC_USERNAME = "x-access-token"


@dataclass
class CredentialSourceSpec:
    """Where the parent process resolves a real secret from.

    The secret is resolved in the *parent* (trusted) process and never
    handed to the sandbox verbatim — only a synthetic placeholder is.

    :param kind: Resolution mode, one of ``"env"``, ``"file"``, or
        ``"command"``.
    :param env: Environment-variable name carrying the secret when
        ``kind="env"``, e.g. ``"OA_TEST_GITHUB_PAT"``.
    :param path: File path to read when ``kind="file"`` (``~`` is
        expanded), e.g. ``"~/.config/tokens/github_pat.txt"``.
    :param command: Shell command whose stdout is the secret when
        ``kind="command"``, e.g. ``"gh auth token"``.
    """

    kind: Literal["env", "file", "command"]
    env: str | None = None
    path: str | None = None
    command: str | None = None


@dataclass
class CredentialProxyEntry:
    """One normalized host binding for the secretless credential proxy.

    Every YAML ``credential_proxy`` type (``https_bearer``,
    ``https_basic``, ``git_https``, ``gh_basic``) is normalized by the
    spec parser into one or more of these entries. The runtime resolves
    :attr:`source` in the parent (it never enters the sandbox) and the
    egress MITM proxy attaches the real credential to outbound requests
    bound for :attr:`host`.

    **Default: swap-on-access.** The sandbox holds *nothing*
    credential-shaped. A tool simply makes its request to :attr:`host`
    with no ``Authorization`` header, and the proxy injects
    ``Authorization: <scheme> <real>`` on the way out. Git over HTTPS,
    ``curl``, and any HTTP client work this way with zero in-sandbox
    wiring.

    **Opt-in: env injection.** Some clients refuse to issue a request
    when they don't see a credential locally — most notably ``gh``,
    which short-circuits with "authentication required" before touching
    the network. For those, :attr:`inject_env` names env vars that
    receive a synthetic ``oa_cred_*`` placeholder so the client believes
    it is authenticated and actually sends the request; the proxy then
    swaps the placeholder for the real secret (and rejects a placeholder
    replayed to any other host with HTTP 403, the cross-host leak guard).
    The placeholder is non-secret — the real secret still never enters
    the sandbox.

    :param host: Exact hostname this binding applies to (lower-cased),
        e.g. ``"github.com"`` or ``"api.github.com"``. Path scoping is
        delegated to ``egress_rules``; the credential binds to the host.
    :param scheme: HTTP ``Authorization`` scheme the proxy emits upstream,
        one of ``"basic"``, ``"bearer"``, or ``"token"``.
    :param source: Where the parent resolves the real secret from.
    :param username: Basic-auth username emitted upstream when
        ``scheme="basic"``, e.g. ``"x-access-token"``. ``None`` for the
        ``bearer`` / ``token`` schemes.
    :param inject_env: Opt-in environment-variable names set to a
        synthetic placeholder inside the sandbox so a credential-gating
        client (e.g. ``gh`` via ``GH_TOKEN`` / ``GITHUB_TOKEN``) will
        emit a request the proxy can rewrite. Empty (the default) means
        pure swap-on-access — nothing is injected and the proxy attaches
        the credential unconditionally for :attr:`host`.
    """

    host: str
    scheme: Literal["basic", "bearer", "token"]
    source: CredentialSourceSpec
    username: str | None = None
    inject_env: list[str] = field(default_factory=list)


@dataclass
class CredentialProxySpec:
    """Secretless credential-proxy policy for a sandbox.

    :param entries: Normalized per-host credential bindings. The real
        secrets stay in the parent; the sandbox only ever sees synthetic
        placeholders that the egress proxy rewrites.
    """

    entries: list[CredentialProxyEntry]


@dataclass
class OSEnvSandboxSpec:
    """Sandbox configuration for an OS environment."""

    # Backend identifier, e.g. ``"linux_bwrap"``,
    # ``"darwin_seatbelt"``, or ``"none"``. The dataclass default of
    # ``"linux_bwrap"`` is a safe sentinel for in-process construction
    # (``OSEnvSandboxSpec(type=self.type_name)`` is the idiomatic call
    # site); YAML parsers map a missing ``type:`` field to the platform
    # default at parse time via
    # :func:`omnigent.inner.sandbox._default_sandbox_for_platform`,
    # which picks ``linux_bwrap`` on Linux (with ``bwrap`` on PATH)
    # and ``darwin_seatbelt`` on macOS.
    type: str = "linux_bwrap"
    # Read-only filesystem grants. Each entry is a path string the
    # backend resolves (``~`` is expanded; ``$VAR`` is intentionally
    # NOT expanded — see ``_resolve_root`` for the rationale).
    #
    # S5 (security): on ``darwin_seatbelt`` and ``linux_bwrap``,
    # granting a path here does NOT silently expose sensitive
    # subtrees that live inside it. Two layers apply on top of the
    # grant:
    #
    # 1. **Dotfile / dotdir masking.** Every ``read_paths`` root is
    #    walked with the same logic ``cwd`` is walked under, and any
    #    dotfile / dotdir whose basename is not in
    #    :attr:`cwd_allow_hidden` is masked. So ``read_paths: ["~/"]``
    #    does NOT expose ``~/.aws/credentials``, ``~/.ssh/id_*``,
    #    ``~/.config/gcloud/`` etc. — those dotdirs are masked just
    #    as they would be under ``cwd``. To grant a dotfile-shaped
    #    path through, list it explicitly in ``read_paths`` AND name
    #    its basename in ``cwd_allow_hidden`` (e.g.
    #    ``read_paths: ["~/.aws"], cwd_allow_hidden: [".aws"]``).
    #
    # 2. **macOS HOME-anchored denylist.** On ``darwin_seatbelt``,
    #    ``$HOME/Library`` is denied by default even when a broader
    #    ``read_paths`` grant covers it. ``~/Library`` holds the
    #    bulk of non-dotfile-shaped macOS credential / personal-data
    #    stores (browser cookies, Slack tokens, Docker keychain,
    #    Messages history, app preferences with stored credentials)
    #    that the dotfile masker can't catch by name. The deny is
    #    suppressed only when ``read_paths`` explicitly names
    #    ``~/Library`` (or any path under it); naming an ANCESTOR
    #    such as ``~/`` does NOT count as opt-in.
    #
    # Both layers honour the spec's ``cwd_hidden_scan_max_entries``
    # / ``cwd_hidden_scan_overflow`` knobs. A spec that grants a
    # huge tree (e.g. ``read_paths: ["~/"]``) will typically trip
    # the entry cap; the resulting :class:`OSError` names the
    # offending root and the tunables.
    read_paths: list[str] | None = None
    write_paths: list[str] | None = None
    # Per-file write grants. Use this for single files that can't be
    # expressed as a directory write path (e.g. ``~/.claude.json``).
    # The bwrap backend treats each entry as an additional
    # ``--bind-try`` file-to-file mount; the parent directory must
    # already be visible inside the sandbox view (typically via a
    # ``read_paths`` / ``write_paths`` grant covering it).
    write_files: list[str] | None = None
    allow_network: bool = True
    # Dotfile/dotdir basenames that pass through the sandbox view at
    # any depth under cwd AND under every spec-supplied
    # ``read_paths`` root. Consumed by both the ``linux_bwrap`` and
    # ``darwin_seatbelt`` backends: bwrap tmpfs-masks every
    # dotfile/dotdir reachable under any walked root whose basename
    # is not in this list, and seatbelt emits per-path ``(deny
    # file-read* file-write* ...)`` SBPL rules for the same set.
    # Matching is by basename, so an entry ``".venv"`` allows
    # ``cwd/.venv``,
    # ``cwd/services/api/.venv``, and ``<read_path>/.venv`` alike.
    # ``None`` means "use the backend's documented default" (both
    # bwrap and seatbelt expand ``None`` to ``[".venv"]`` so a
    # typical Python project keeps working out of the box). An empty
    # list means "mask every dotfile" — there is no implicit default
    # when the field is set. Naming a credential-shaped basename
    # here (``".aws"``, ``".ssh"``, etc.) is the explicit opt-in
    # path for granting credential dotdirs through a ``read_paths``
    # entry.
    cwd_allow_hidden: list[str] | None = None
    # Maximum number of filesystem entries the shared dotfile/symlink
    # masker (used by both ``linux_bwrap`` and ``darwin_seatbelt``)
    # will visit while walking cwd. The walk prunes at masked
    # dot-directories so realistic projects fit well under the
    # default; ``node_modules``, ``target``, etc. that aren't
    # dot-prefixed do count. Behaviour when the cap is reached is
    # controlled by :attr:`cwd_hidden_scan_overflow`. Ignored by
    # backends that don't do filesystem masking.
    cwd_hidden_scan_max_entries: int = 50000
    # What to do when :attr:`cwd_hidden_scan_max_entries` is reached:
    # - ``"warn"`` (default): emit a logging warning, stop scanning, and
    #   return the partial mask. Dotfiles past the cap remain visible.
    #   This is the default because realistic projects (notably ones
    #   carrying a ``node_modules`` tree) routinely blow past the cap,
    #   and a hard failure that blocks every spawn is worse than a
    #   best-effort mask for the typical trusted-workspace case. The
    #   walker deprioritizes ``node_modules`` so the budget is spent on
    #   the rest of the tree first (see :mod:`omnigent.inner._cwd_scan`).
    # - ``"error"``: the resolver raises ``OSError`` so the user notices
    #   and tunes the limit explicitly. Fail-Loud — the right pick for
    #   untrusted source trees where an unmasked dotfile is a leak.
    # - ``"unlimited"``: ignore the cap and walk the entire tree —
    #   slow on large monorepos but fully hermetic.
    #
    # L6 (security trade-off): ``"warn"`` and ``"unlimited"`` both
    # have security implications operators should understand:
    #
    # - ``"warn"`` is an availability-over-security choice. Dotfiles
    #   past the cap remain readable by the sandboxed agent. If a
    #   credential file (``.aws/credentials``, ``.netrc``, ``.env``,
    #   ``.ssh/id_*``) sits past the cap — e.g. checked into a
    #   ``node_modules`` subtree, or planted by a compromised tool
    #   call earlier in the session — the sandbox will NOT mask it.
    #   The trade-off is acceptable when the operator knows the
    #   workspace doesn't carry secrets and a partial mask is
    #   preferable to an outright failure. A ``CRITICAL`` log line
    #   is emitted on every overflow so this choice is auditable.
    #
    # - ``"unlimited"`` removes the cap entirely. Safer from a
    #   masking standpoint (the whole tree is walked) but lets a
    #   malicious workspace bomb the resolver — a tarbomb with
    #   millions of dot-files would burn CPU and emit a huge SBPL /
    #   bwrap-arg list. The :data:`_MAX_PROFILE_BYTES` cap (256 KiB
    #   on seatbelt) still fail-loud catches the runaway, but the
    #   walker can spend minutes getting there.
    #
    # The ``"warn"`` default favors availability for the common
    # trusted-workspace case. Switch to ``"error"`` for hostile inputs
    # (untrusted source trees, supervisor-spawned forks) where an
    # unmasked dotfile past the cap would be an unacceptable leak.
    cwd_hidden_scan_overflow: str = "warn"
    # Environment-variable allowlist for the helper subprocess, beyond
    # the always-passed minimal default (PATH/HOME/USER/LANG/LC_*/etc.;
    # see :data:`omnigent.inner.os_env._DEFAULT_ENV_PASSTHROUGH`).
    # The parent process inherits the user's full shell environment,
    # which typically includes credentials like ``AWS_ACCESS_KEY_ID``,
    # ``GITHUB_TOKEN``, ``OPENAI_API_KEY``, etc. — passing all of that
    # to the helper would defeat the filesystem masking that hides
    # ``~/.aws/credentials`` and friends, since the helper could just
    # ``sys_os_shell("env")`` to enumerate them and exfiltrate over
    # the (default-shared) network.
    #
    # Each entry is an exact variable name (no globs) that the helper
    # is allowed to inherit. Names not in the default set or this list
    # are stripped from the helper's environment before spawn. ``None``
    # means "allow only the defaults"; an empty list is identical
    # ("allow only the defaults"). Use this field to grant specific
    # secrets the agent legitimately needs, e.g.
    # ``env_passthrough: ["AWS_PROFILE", "GITHUB_TOKEN"]``.
    env_passthrough: list[str] | None = None
    # L7 egress policy rules. When non-empty, a MITM proxy is started
    # and all HTTP(S) traffic from the helper is routed through it.
    # Each rule is a string in the DSL: ``"METHODS host/path/glob"``
    # (e.g. ``"GET api.github.com/repos/org/**"``). Default deny —
    # requests not matching any rule are blocked with HTTP 403.
    # Requires a backend that can hard-enforce network isolation at
    # spawn time so the MITM proxy is the only egress path. Both
    # ``linux_bwrap`` (isolated network namespace via
    # ``--unshare-net``) and ``darwin_seatbelt`` (SBPL
    # ``(deny network*)`` with narrow allows for the loopback relay
    # and the parent's Unix socket) satisfy this. The parser rejects
    # ``egress_rules`` on every other backend type.
    egress_rules: list[str] | None = None
    # When ``False`` (the default), the egress proxy refuses to
    # open upstream connections to addresses that are not globally
    # routable — RFC1918 private ranges, loopback, link-local, IPv6
    # ULA, CGNAT / RFC 6598 (``100.64.0.0/10``), reserved blocks,
    # TEST-NETs, IETF benchmark range, and multicast. Also refuses
    # known cloud-internal IPs that present as public but are
    # routed only inside the tenant — currently Azure WireServer
    # ``168.63.129.16`` (see ``_CLOUD_TRAP_NETWORKS`` in
    # ``omnigent/inner/egress/proxy.py``). Defends against
    # DNS-rebinding attacks where the agent uses a wildcard rule
    # like ``GET *.example.com/**`` and a subdomain it controls
    # resolves to ``127.0.0.1`` (reaching the parent's localhost
    # services), ``10.0.0.5`` (VPC internals),
    # ``169.254.169.254`` (cloud IMDS), ``100.100.100.200``
    # (Alibaba IMDS, in CGNAT), or ``168.63.129.16`` (Azure
    # WireServer). Applies to every sandbox backend; only takes
    # effect when ``egress_rules`` is configured (the only path
    # that starts the MITM proxy). Set to ``True`` to allow agents
    # to reach internal corp services on private IPs — auditable
    # opt-in since "talks to intranet" is a real workload and not
    # always an attack. Note: the opt-in lifts *every* part of
    # this check including the cloud-trap list, so flip it on only
    # for workloads that genuinely need cloud-host metadata.
    egress_allow_private_destinations: bool = False
    # Optional secretless credential-proxy policy. Real tokens stay in
    # the parent process and are attached to outbound requests by the
    # egress MITM proxy. By default the sandbox holds nothing
    # credential-shaped at all (swap-on-access): a tool makes its
    # request with no ``Authorization`` header and the proxy injects the
    # real credential for the bound host. Entries may opt into injecting
    # a synthetic ``oa_cred_*`` placeholder env var for clients that
    # won't issue a request without a local credential (e.g. ``gh``).
    # Requires ``egress_rules`` (the proxy is what attaches the
    # credential and rejects placeholder leaks) and a backend that
    # hard-isolates the network (``linux_bwrap`` / ``darwin_seatbelt``).
    credential_proxy: CredentialProxySpec | None = None


@dataclass
class OSEnvSpec:
    """Configuration for an operating system environment.

    :param start_in_scratch: When ``True``, the helper starts
        inside the writable scratch tmpdir instead of cwd. The
        workspace remains bound read-only so the agent can reach
        project files via absolute paths. Requires an active
        sandbox (rejected for ``type='none'``). Mutually exclusive
        with :attr:`fork`. Default ``False``.
    """

    type: str = "caller_process"
    cwd: str | None = None
    sandbox: OSEnvSandboxSpec | None = None
    fork: bool = False
    start_in_scratch: bool = False
    # createos provider fields (ignored unless type='createos')
    createos_base_url: str | None = None
    createos_api_key: str | None = None
    createos_shape: str | None = None
    createos_rootfs: str | None = None


@dataclass
class TerminalEnvSpec:
    """
    Configuration for a terminal environment.

    :param command: Program to run in the terminal, e.g. ``"bash"``.
    :param args: Command arguments, e.g. ``["-lc", "echo hi"]``.
    :param env: Extra environment variables for the terminal process,
        e.g. ``{"CODEX_HOME": "/tmp/codex-home"}``.
    :param env_unset: Environment variables to strip from the
        terminal's environment before launching, e.g.
        ``["DATABRICKS_CONFIG_PROFILE"]``. Applied AFTER ``env``
        is merged, so a listed key is removed unconditionally —
        if the same key also appears in ``env``, the strip wins.
        This is intentional: ``env_unset`` is a leak-prevention
        boundary, not a soft default, so the property "this key
        does not reach the terminal" holds regardless of upstream
        ``env`` composition. Used when ambient host env vars would
        mis-configure the terminal's child processes (for example,
        MCP servers that construct Databricks SDK clients and let
        the SDK's auth resolver pick up the parent's profile
        instead of the explicit token they were given).
    :param os_env: OS environment backing this terminal, ``"inherit"``,
        or ``None`` to use the default caller process environment.
    :param allow_cwd_override: Whether launch callers may override cwd.
    :param allow_sandbox_override: Whether launch callers may override
        sandbox type.
    :param log_file: Optional terminal log path.
    :param scrollback: Tmux scrollback history limit, e.g. ``10000``.
    :param session_prefix: Prefix used by legacy terminal sessions.
    :param tmux_allow_passthrough: Enable tmux passthrough escape
        sequences for this terminal. Used for TUIs that need to query
        or control the real attached terminal.
    :param tmux_start_on_attach: Delay the terminal command until the
        first tmux client attaches. Used for TUIs that must query the
        real attached terminal during startup.
    :param keep_alive_after_exit: Keep the private tmux server alive after
        the pane's inner process exits (``remain-on-exit`` / ``exit-empty
        off``), so a single CLI exit no longer reaps the server and cascades
        into ``no server running``. Opt-in because it changes the
        ``has-session``-means-alive contract; enabled for the claude-native
        agent terminal (#540), whose liveness is decided by ``#{pane_dead}``.
    """

    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_unset: list[str] = field(default_factory=list)
    os_env: OSEnvSpec | str | None = None
    allow_cwd_override: bool = False
    allow_sandbox_override: bool = False
    log_file: str | None = None
    scrollback: int = 10000
    session_prefix: str = "omni_"
    tmux_allow_passthrough: bool = False
    tmux_start_on_attach: bool = False
    keep_alive_after_exit: bool = False


# ---------------------------------------------------------------------------
# AgentDef
# ---------------------------------------------------------------------------


@dataclass
class AgentDef:
    """A declarative specification for an agent.

    :param name: Agent identifier used for logging, spawn routing,
        and sub-agent naming, e.g. ``"coder"``. ``None`` when the
        agent is anonymous (e.g. an inline sub-agent).
    :param prompt: System prompt supplied to the executor. ``None``
        when the agent has no prompt (e.g. pure tool-routing agents).
    :param instructions: Resolved system-prompt text loaded from
        the YAML's ``instructions:`` key (a path relative to the
        YAML directory, falling through to inline text). ``None``
        when no ``instructions:`` field is present. When set,
        takes precedence over :attr:`prompt` at translation time.
        See :func:`omnigent.inner.loader.load_agent_def` for
        the path-resolution rules.
    """

    name: str | None = None
    prompt: str | None = None
    instructions: str | None = None
    tools: ToolRegistry = field(default_factory=dict)  # str -> Tool
    input_type: str | None = None
    output_type: str | None = None
    executor: ExecutorSpec | None = None
    policies: PolicyRegistry = field(default_factory=dict)  # str -> Policy
    params: dict[str, ParamDef] = field(default_factory=dict)
    memories: dict[str, MemoryConfig] = field(default_factory=dict)
    async_enabled: bool = True
    cancellable: bool = True
    runtime: bool = False
    timers: bool = False
    # Grant for spawning OUTSIDE any declared sub-agent list:
    # sys_session_create (existing agents by id, or custom bundles via
    # config_path) plus send/close to drive the children. Distinct
    # from declared agent tools, which permit only the specified
    # sub-agent types. Session reads are always on. YAML key:
    # ``spawn:``.
    spawn: bool = False
    # Authority for the agent to share the session it runs in, via
    # sys_session_share — the SOLE enabler of that tool (independent of
    # spawn / declared agents, and unrelated to server-API / CLI
    # sharing). Raw YAML string from ``agent_session_sharing:`` — "none"
    # (default, tool off), "non-public" (grant named users), or "public"
    # (also allow __public__ anonymous read). Kept as a str here (inner
    # datamodel has no spec.types dep); mapped to SharePolicy when
    # translated to an AgentSpec.
    agent_session_sharing: str = "none"
    os_env: OSEnvSpec | None = None
    terminals: dict[str, TerminalEnvSpec] = field(default_factory=dict)
    skills: SkillRegistry = field(default_factory=dict)
    # Materialized agent-bundle root on disk, when known. Used by
    # the Claude SDK harness to expose ``<bundle>/skills/<name>/
    # SKILL.md`` files as plugin skills via the SDK's
    # ``--plugin-dir`` mechanism. Set by the AgentSpec → AgentDef
    # bridge from the spec's parsed ``skill_dir`` paths; left
    # ``None`` for in-memory ``AgentDef`` (tests, code-built
    # agents) and for specs whose bundle has no
    # ``skills/`` directory.
    bundle_dir: Path | None = None
    # Host-skill filter (``"all"`` / ``"none"`` / ``list[str]``)
    # mapped from the top-level YAML ``skills:`` field. The Claude
    # SDK harness translates this into the SDK's ``skills``
    # option; other harnesses ignore it. Defaults to ``"all"`` —
    # every host-discovered skill is exposed by default; agents
    # that want to be hermetic against the user's local skill
    # library set this to ``"none"`` (or list explicit names).
    skills_filter: str | list[str] = "all"
    workflow: Workflow = None  # str (DAG expression) or Callable
    metadata: AgentMetadata = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    label_schema: dict[str, LabelSchemaRule] = field(default_factory=dict)
    ask_timeout: float | None = None  # seconds, None = wait forever
    policy_transparency: bool = False

    @staticmethod
    def from_yaml(path: str) -> AgentDef:
        """Load an AgentDef from a YAML file.

        Convenience method matching the design doc API::

            agent = AgentDef.from_yaml("agent.yaml")
        """
        from .loader import load_agent_def

        return load_agent_def(path)


@dataclass
class LabelSchemaRule:
    """Schema and propagation constraints for a session label.

    ``monotonic`` controls both the write direction and child-to-parent
    propagation:
    - ``"max"``: value can only increase; child propagation takes the max.
    - ``"min"``: value can only decrease; child propagation takes the min.
    - ``"none"``: value can change freely; no child propagation.
    """

    values: list[str] = field(default_factory=list)
    monotonic: str = "none"  # "max", "min", or "none"

    def normalize(self, value: LabelValue) -> str | None:
        candidate = str(value)
        return candidate if candidate in self.values else None

    def allows(self, current: str | None, new_value: str) -> bool:
        if new_value not in self.values:
            return False
        if current is None:
            return True
        if current not in self.values:
            return False

        current_idx = self.values.index(current)
        new_idx = self.values.index(new_value)
        if self.monotonic == "max":
            return new_idx >= current_idx
        if self.monotonic == "min":
            return new_idx <= current_idx
        return True

    def merged_with_child(self, parent_val: str | None, child_val: str | None) -> str | None:
        """Merge parent and child values according to the monotonic rule."""
        if self.monotonic == "none":
            return None
        if parent_val is None and child_val is None:
            return None
        if parent_val is None:
            if child_val is None:
                return None
            if child_val not in self.values:
                raise ValueError(f"Unknown child label value during propagation: {child_val!r}")
            return child_val
        if parent_val not in self.values:
            raise ValueError(f"Unknown parent label value during propagation: {parent_val!r}")
        if child_val is None:
            return parent_val
        if child_val not in self.values:
            raise ValueError(f"Unknown child label value during propagation: {child_val!r}")

        parent_idx = self.values.index(parent_val)
        child_idx = self.values.index(child_val)
        if self.monotonic == "max":
            return self.values[max(parent_idx, child_idx)]
        if self.monotonic == "min":
            return self.values[min(parent_idx, child_idx)]
        raise ValueError(f"Unknown monotonic mode: {self.monotonic!r}")
