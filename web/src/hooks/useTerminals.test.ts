// Unit tests for the terminal resource mappers and the HTTP seed fetch.
//
// `terminalInfoFromResource` maps a wire ``session.resource`` dict onto
// the UI-facing TerminalInfo (used by both the chatStore SSE handler and
// the seed fetch). `fetchTerminals` is the authoritative HTTP seed that
// `useTerminals` runs on mount so the Terminal pill reflects an
// already-running terminal on load / after a missed SSE event.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createTerminal,
  deleteTerminal,
  fetchTerminals,
  findAgentTerminal,
  inventoryTerminals,
  isAgentTerminalKey,
  PENDING_RECONCILE_INTERVAL_MS,
  terminalInfoFromResource,
  terminalsReconcileInterval,
  terminalTabKey,
  useTerminals,
  type TerminalInfo,
} from "./useTerminals";
import {
  clearDirectAttachCaches,
  directProbeUrl,
  resolveInitialAttachUrl,
  watchDirectUpgrade,
  withAttachParams,
} from "@/lib/terminals";

// useTerminals reads runner liveness to treat an offline runner as zero
// terminals. Mock it so we can drive that signal directly; it defaults to
// `undefined` (the no-provider value), which leaves the other tests'
// behavior unchanged.
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(() => undefined),
}));
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";

const runnerOnlineMock = vi.mocked(useSessionRunnerOnline);

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

describe("terminalInfoFromResource", () => {
  it("lifts id, metadata.terminal_name, metadata.session_key, metadata.running", () => {
    const info = terminalInfoFromResource({
      id: "terminal_bash_s1",
      object: "session.resource",
      type: "terminal",
      session_id: "conv_abc",
      name: "bash:s1",
      environment: "env_terminal_bash_s1",
      metadata: { terminal_name: "bash", session_key: "s1", running: true },
    });
    expect(info).not.toBeNull();
    const t = info as TerminalInfo;
    expect(t.id).toBe("terminal_bash_s1");
    expect(t.name).toBe("bash");
    expect(t.session).toBe("s1");
    expect(t.running).toBe(true);
  });

  it("falls back to resource.name when metadata.terminal_name is absent", () => {
    // The server should always supply metadata.terminal_name, but a
    // thin event from a legacy producer must not break the rail.
    const t = terminalInfoFromResource({
      id: "terminal_x",
      type: "terminal",
      session_id: "conv_abc",
      name: "fallback-name",
      metadata: {},
    });
    expect(t?.name).toBe("fallback-name");
    expect(t?.session).toBe("");
    expect(t?.running).toBe(false);
  });

  it("returns null when the resource has no id", () => {
    // applyTerminalCreated drops a resource that can't be addressed
    // by id rather than inserting an unkeyable entry.
    const info = terminalInfoFromResource({
      type: "terminal",
      name: "bash:s1",
      metadata: {},
    });
    expect(info).toBeNull();
  });

  it("lifts metadata.terminal_transport, defaulting to undefined for unknown values", () => {
    const control = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "control" },
    });
    expect(control?.transport).toBe("control");

    const pty = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "pty" },
    });
    expect(pty?.transport).toBe("pty");

    // Absent or unrecognized → undefined (frontend treats as legacy PTY).
    const legacy = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: {},
    });
    expect(legacy?.transport).toBeUndefined();
    const junk = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "garbage" },
    });
    expect(junk?.transport).toBeUndefined();
  });

  it("lifts metadata.direct_attach_url only for loopback ws:// URLs", () => {
    const direct = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { direct_attach_url: "ws://127.0.0.1:54321/v1/x/attach?token=t" },
    });
    expect(direct?.directAttachUrl).toBe("ws://127.0.0.1:54321/v1/x/attach?token=t");

    // Anything that could steer the terminal socket at a non-loopback
    // host is dropped, even from our own server.
    for (const bad of [
      "wss://127.0.0.1:54321/attach?token=t",
      "ws://evil.example:54321/attach?token=t",
      "ws://localhost:54321/attach?token=t",
      42,
      null,
    ]) {
      const info = terminalInfoFromResource({
        id: "terminal_bash_s1",
        type: "terminal",
        name: "bash:s1",
        metadata: { direct_attach_url: bad },
      });
      expect(info?.directAttachUrl).toBeUndefined();
    }
  });
});

describe("direct attach URL helpers", () => {
  beforeEach(() => {
    clearDirectAttachCaches();
  });

  it("withAttachParams appends per-attach params after the token", () => {
    const base = "ws://127.0.0.1:54321/v1/x/attach?token=t";
    expect(withAttachParams(base, false, undefined)).toBe(base);
    expect(withAttachParams(base, true, undefined)).toBe(`${base}&read_only=true`);
    expect(withAttachParams(base, false, "control")).toBe(`${base}&transport=control`);
    expect(withAttachParams(base, true, "pty")).toBe(`${base}&read_only=true&transport=pty`);
  });

  it("directProbeUrl swaps the path for /probe and keeps the token", () => {
    expect(directProbeUrl("ws://127.0.0.1:54321/v1/x/attach?token=t&read_only=true")).toBe(
      "ws://127.0.0.1:54321/probe?token=t&read_only=true",
    );
    expect(directProbeUrl("not a url")).toBeNull();
  });
});

describe("resolveInitialAttachUrl / watchDirectUpgrade", () => {
  /** WebSocket stub whose fate is decided by the test. */
  class FakeWebSocket {
    static instances: FakeWebSocket[] = [];
    static behavior: "open" | "error" | "hang" = "open";
    url: string;
    closed = false;
    private listeners = new Map<string, () => void>();
    constructor(url: string) {
      this.url = url;
      FakeWebSocket.instances.push(this);
      queueMicrotask(() => {
        const behavior = FakeWebSocket.behavior;
        if (behavior === "hang") return;
        this.listeners.get(behavior === "open" ? "open" : "error")?.();
      });
    }
    addEventListener(type: string, listener: () => void): void {
      this.listeners.set(type, listener);
    }
    close(): void {
      this.closed = true;
    }
  }

  /** Stub the Permissions API's local-network-access answer. */
  function stubPermission(state: "granted" | "denied" | "prompt" | "missing"): void {
    const base = globalThis.navigator ?? {};
    const permissions =
      state === "missing"
        ? undefined
        : {
            query: async (desc: { name: string }) => {
              expect(desc.name).toBe("local-network-access");
              return { state };
            },
          };
    vi.stubGlobal("navigator", Object.assign(Object.create(base), { permissions }));
  }

  beforeEach(() => {
    clearDirectAttachCaches();
    FakeWebSocket.instances = [];
    FakeWebSocket.behavior = "open";
    vi.stubGlobal("WebSocket", FakeWebSocket);
    stubPermission("missing");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const DIRECT = "ws://127.0.0.1:54321/v1/x/attach?token=t";
  const RELAY = "wss://server.example/v1/x/attach";

  it("returns the relay URL when no direct URL is offered", async () => {
    expect(await resolveInitialAttachUrl(undefined, RELAY)).toBe(RELAY);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("returns the relay immediately when permission state is unknown or prompt", async () => {
    // Never stall the first paint behind a permission prompt: the
    // initial dial is relay; the upgrade happens in the background.
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(RELAY);
    expect(FakeWebSocket.instances).toHaveLength(0);
    stubPermission("prompt");
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(RELAY);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("dials direct on first connect when permission is granted and the probe opens", async () => {
    stubPermission("granted");
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(DIRECT);
    // The probe dialed the side-effect-free /probe route, not the
    // attach route, and closed its socket after settling.
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.instances[0].url).toBe("ws://127.0.0.1:54321/probe?token=t");
    expect(FakeWebSocket.instances[0].closed).toBe(true);
  });

  it("falls back to the relay when granted but nothing is listening", async () => {
    stubPermission("granted");
    FakeWebSocket.behavior = "error";
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(RELAY);
  });

  it("watchDirectUpgrade succeeds on Allow and primes the next dial to go direct", async () => {
    // Prompt state: the background probe carries the LNA prompt; the
    // user clicking Allow completes the handshake.
    stubPermission("prompt");
    expect(await watchDirectUpgrade(DIRECT)).toBe(true);
    // The known-good cache now routes the re-dial straight to direct
    // without consulting the permission API again.
    stubPermission("missing");
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(DIRECT);
  });

  it("watchDirectUpgrade skips probing entirely when permission is denied", async () => {
    stubPermission("denied");
    expect(await watchDirectUpgrade(DIRECT)).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("watchDirectUpgrade settles false on abort and on a blocked handshake", async () => {
    FakeWebSocket.behavior = "hang";
    const ctl = new AbortController();
    const pending = watchDirectUpgrade(DIRECT, ctl.signal);
    await Promise.resolve();
    ctl.abort();
    expect(await pending).toBe(false);
    expect(FakeWebSocket.instances[0].closed).toBe(true);

    FakeWebSocket.behavior = "error";
    expect(await watchDirectUpgrade(DIRECT)).toBe(false);
  });

  it("falls back to the relay when the WebSocket constructor throws", async () => {
    // Safari throws a synchronous SecurityError for ws:// from an
    // https page; the resolver must treat it as an ordinary miss.
    stubPermission("granted");
    vi.stubGlobal("WebSocket", function ThrowingWebSocket() {
      throw new Error("SecurityError");
    });
    expect(await resolveInitialAttachUrl(DIRECT, RELAY)).toBe(RELAY);
  });
});

describe("fetchTerminals", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GETs the terminals list and maps each row to TerminalInfo", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        object: "list",
        data: [
          {
            id: "terminal_codex_main",
            type: "terminal",
            session_id: "conv_abc",
            name: "codex:main",
            metadata: { terminal_name: "codex", session_key: "main", running: true },
          },
        ],
      }),
    );
    const out = await fetchTerminals("conv_abc");
    // Proves the seed both hits the right endpoint and lands a usable
    // entry — len-only would pass even if mapping produced empty fields.
    // `order=asc` is load-bearing: the endpoint defaults to desc
    // (newest first), which on refresh would bump the session's own
    // terminal (created first) out of the first tab slot; asc keeps
    // the seed consistent with SSE created-deltas appending at the end.
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_abc/resources/terminals?order=asc&limit=1000",
      expect.anything(),
    );
    expect(out).toEqual([
      { id: "terminal_codex_main", name: "codex", session: "main", running: true },
    ]);
  });

  it.each([404, 409, 502, 503])(
    "returns [] for a not-yet-reachable runner (%i)",
    async (status) => {
      // These are "no terminal yet", not errors: the live SSE event fills
      // the rail once the runner binds, so the seed must not throw.
      fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status }));
      expect(await fetchTerminals("conv_abc")).toEqual([]);
    },
  );

  it("throws on a hard error status so React Query can retry", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status: 500 }));
    await expect(fetchTerminals("conv_abc")).rejects.toThrow(/500/);
  });
});

describe("createTerminal", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs the declared name with a fresh u- session key and maps the resource", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        id: "terminal_shell_u-abc123",
        object: "session.resource",
        type: "terminal",
        session_id: "conv_abc",
        name: "shell:u-abc123",
        metadata: { terminal_name: "shell", session_key: "u-abc123", running: true },
      }),
    );

    const out = await createTerminal("conv_abc", "shell");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/resources/terminals");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string) as { terminal: string; session_key: string };
    expect(body.terminal).toBe("shell");
    // A fresh random `u-` key per call: the runner's launch is
    // idempotent per (terminal, session_key), so a fixed key would
    // return the SAME terminal on every click instead of a new one.
    expect(body.session_key).toMatch(/^u-/);
    // Mapped through terminalInfoFromResource — proves the POST
    // response shape lands as a usable TerminalInfo, not raw wire.
    expect(out).toEqual({
      id: "terminal_shell_u-abc123",
      name: "shell",
      session: "u-abc123",
      running: true,
    });
  });

  it("surfaces the server gate's message on a 400 rejection", async () => {
    // The server's iff gate (agent has no terminals: block / name not
    // declared) returns a structured error; the UI must surface that
    // message, not a generic status line.
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "invalid_input", message: "Terminal 'zsh' is not declared" } },
        { ok: false, status: 400 },
      ),
    );
    await expect(createTerminal("conv_abc", "zsh")).rejects.toThrow(/not declared/);
  });
});

describe("deleteTerminal", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("DELETEs the terminal resource by id", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: true, status: 204 }));

    await deleteTerminal("conv_abc", "terminal_bash_s1");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/resources/terminals/terminal_bash_s1");
    expect(init.method).toBe("DELETE");
  });

  it("treats a 404 (already gone) as success", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status: 404 }));
    await expect(deleteTerminal("conv_abc", "terminal_gone")).resolves.toBeUndefined();
  });

  it("throws on a non-404 failure", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status: 500 }));
    await expect(deleteTerminal("conv_abc", "terminal_bash_s1")).rejects.toThrow(/delete failed/);
  });
});

describe("terminalsReconcileInterval", () => {
  it("polls only while pending AND no agent terminal is visible yet", () => {
    expect(terminalsReconcileInterval(true, false)).toBe(PENDING_RECONCILE_INTERVAL_MS);
  });

  it("stops the instant the agent terminal lands", () => {
    expect(terminalsReconcileInterval(true, true)).toBe(false);
  });

  it("never polls when the runner is not spinning up a terminal", () => {
    expect(terminalsReconcileInterval(false, false)).toBe(false);
    expect(terminalsReconcileInterval(false, true)).toBe(false);
  });
});

describe("useTerminals reconcile poll (stuck-spinner self-heal)", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function makeWrapper() {
    // A fresh client per render so query cache never leaks between tests.
    // (The hook hard-codes retry:1, which this default does not override; the
    // mocks below always resolve, so no retry/backoff fires under fake timers.)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
  }

  const emptyList = () => mockResponse({ object: "list", data: [] });
  const oneShell = () =>
    mockResponse({
      object: "list",
      data: [
        {
          id: "terminal_bash_s1",
          type: "terminal",
          session_id: "conv_abc",
          name: "bash:s1",
          metadata: { terminal_name: "bash", session_key: "s1", running: true },
        },
      ],
    });
  const oneTerminal = () =>
    mockResponse({
      object: "list",
      data: [
        {
          id: "terminal_claude_main",
          type: "terminal",
          session_id: "conv_abc",
          name: "claude:main",
          metadata: { terminal_name: "claude", session_key: "main", running: true },
        },
      ],
    });

  it("re-polls while pending until the agent terminal lands, then stops", async () => {
    // A user shell alone must not satisfy the pending agent-terminal launch.
    fetchMock
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(oneShell())
      .mockResolvedValue(oneTerminal());

    const { result } = renderHook(() => useTerminals("conv_abc", { reconcileWhilePending: true }), {
      wrapper: makeWrapper(),
    });

    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(result.current.terminals).toEqual([]);
    const seedCalls = fetchMock.mock.calls.length;

    // The first interval starts reconciliation. Its exact settle point is a
    // React Query scheduling detail, so assert that a refetch was issued.
    await act(async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS)));
    expect(fetchMock.mock.calls.length).toBeGreaterThan(seedCalls);

    // A later poll finds the agent terminal and unions it with the cached shell.
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 2)),
    );
    expect(result.current.terminals).toEqual([
      { id: "terminal_bash_s1", name: "bash", session: "s1", running: true },
      { id: "terminal_claude_main", name: "claude", session: "main", running: true },
    ]);

    // The agent terminal stops the interval; shells alone did not.
    const callsAtLand = fetchMock.mock.calls.length;
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 4)),
    );
    expect(fetchMock.mock.calls.length).toBe(callsAtLand);
  });

  it("does not poll when reconcileWhilePending is false (no terminal yet)", async () => {
    fetchMock.mockResolvedValue(emptyList());

    renderHook(() => useTerminals("conv_abc", { reconcileWhilePending: false }), {
      wrapper: makeWrapper(),
    });

    // Exactly one fetch (the mount seed) and never more: with pending false,
    // refetchInterval returns false so no interval is ever scheduled. A 2nd
    // fetch here would mean an orchestrator/non-terminal session (no terminal
    // expected) was being polled — the regression this guards against.
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 5)),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("useTerminals — SSE-primary list, poll corrects on edges", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    runnerOnlineMock.mockReturnValue(undefined);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    runnerOnlineMock.mockReturnValue(undefined);
  });

  function makeClientWrapper() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    return { client, wrapper };
  }

  const TERMINAL = { id: "terminal_claude_main", name: "claude", session: "main", running: true };
  const emptyList = () => mockResponse({ object: "list", data: [] });
  const oneTerminal = () =>
    mockResponse({
      object: "list",
      data: [
        {
          id: "terminal_claude_main",
          type: "terminal",
          session_id: "conv_abc",
          name: "claude:main",
          metadata: { terminal_name: "claude", session_key: "main", running: true },
        },
      ],
    });

  it("shows a cached terminal immediately even while the poll still reads offline (boot lag)", async () => {
    // SSE-primary: a terminal in the cache (here via the mount seed; in
    // production also via a live `session.resource.created`) is openable right
    // away even though runner liveness still reads `false` during the cold-boot
    // poll lag. A continuous offline mask would hide it until the next poll —
    // the "terminal never clickable" bug. `undefined → false` is NOT a
    // was-online edge, so no correction clears it either.
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(false);

    const { result } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));

    expect(result.current.terminals).toEqual([TERMINAL]);
  });

  it("clears terminals on a confirmed stop (true → false edge)", async () => {
    // The poll's subtractive correction: a running runner's terminal is shown,
    // then the poll confirms the runner went offline. Its PTYs are gone but a
    // stop emits no `session.resource.deleted`, so the SSE list would keep
    // showing dead terminals — the hook must drop them. This is what greys the
    // pill after a Stop. Gated on the was-online edge so it never fires during
    // the cold-boot `undefined → false` window (covered by the test above).
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(true);

    const { result, rerender } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    // Online: the seeded terminal is shown.
    expect(result.current.terminals).toEqual([TERMINAL]);

    // Runner stops: the true → false edge clears the now-dead terminal.
    runnerOnlineMock.mockReturnValue(false);
    await act(async () => {
      rerender();
      await vi.runAllTimersAsync();
    });
    await act(async () => void rerender());
    expect(result.current.terminals).toEqual([]);
  });

  it("re-reads the endpoint on the → online edge to recover a missed created event", async () => {
    // SSE-primary, but if the one-shot `resource.created` was dropped the cache
    // stays empty. The poll's additive correction: on the `→ true` edge the
    // hook re-reads the authoritative endpoint and unions in the terminal it
    // now reports. Start empty (endpoint has nothing, runner unseen), then the
    // runner comes online with a terminal present.
    fetchMock.mockResolvedValue(emptyList());
    runnerOnlineMock.mockReturnValue(undefined);

    const { result, rerender } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    // Nothing yet: empty seed, runner not observed.
    expect(result.current.terminals).toEqual([]);

    // Runner comes online AND the endpoint now reports the terminal: the
    // → true edge re-reads and surfaces it. Without that edge re-read the
    // dropped `created` would leave the pill grey (staleTime: Infinity).
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(true);
    await act(async () => {
      rerender();
      await vi.runAllTimersAsync();
    });
    await act(async () => void rerender());
    expect(result.current.terminals).toEqual([TERMINAL]);
  });
});

describe("terminalTabKey", () => {
  it("keys off the opaque resource id, not display fields", () => {
    const t: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };
    expect(terminalTabKey(t)).toBe("terminal:terminal_bash_s1");
  });

  it("is stable across display-field changes", () => {
    // The tab key must survive a rename / metadata churn so the
    // active tab doesn't jump when the server updates display fields.
    const before: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };
    const after: TerminalInfo = { ...before, name: "bash-renamed", session: "s2" };
    expect(terminalTabKey(before)).toBe(terminalTabKey(after));
  });
});

describe("inventoryTerminals", () => {
  const repl: TerminalInfo = {
    id: "terminal_tui_main",
    name: "tui",
    session: "main",
    running: true,
  };
  const claudePane: TerminalInfo = {
    id: "terminal_claude_main",
    name: "claude",
    session: "main",
    running: true,
  };
  const piPane: TerminalInfo = {
    id: "terminal_pi_main",
    name: "pi",
    session: "main",
    running: true,
  };
  const cursorPane: TerminalInfo = {
    id: "terminal_cursor_main",
    name: "cursor",
    session: "main",
    running: true,
  };
  const kiroPane: TerminalInfo = {
    id: "terminal_kiro_main",
    name: "kiro",
    session: "main",
    running: true,
  };
  const goosePane: TerminalInfo = {
    id: "terminal_goose_main",
    name: "goose",
    session: "main",
    running: true,
  };
  const qwenPane: TerminalInfo = {
    id: "terminal_qwen_main",
    name: "qwen",
    session: "main",
    running: true,
  };
  const kimiPane: TerminalInfo = {
    id: "terminal_kimi_main",
    name: "kimi",
    session: "main",
    running: true,
  };
  const hermesPane: TerminalInfo = {
    id: "terminal_hermes_main",
    name: "hermes",
    session: "main",
    running: true,
  };
  const antigravityPane: TerminalInfo = {
    id: "terminal_antigravity_main",
    name: "antigravity",
    session: "main",
    running: true,
  };
  const bash: TerminalInfo = {
    id: "terminal_bash_s1",
    name: "bash",
    session: "s1",
    running: true,
  };

  it("drops the pi vendor pane for native Pi sessions", () => {
    // Regression: terminal_pi_main was missing from AGENT_TERMINAL_IDS, so
    // the pi pane leaked into the Shells inventory and (via isShellView) hid
    // the Chat/Terminal pill in Terminal view — stranding the user.
    expect(inventoryTerminals([piPane, bash], true)).toEqual([bash]);
  });

  it("drops the cursor vendor pane for native Cursor sessions", () => {
    // Regression: terminal_cursor_main was missing from AGENT_TERMINAL_IDS,
    // same failure mode as the pi pane above — leaked into Shells and hid
    // the Chat/Terminal pill in Terminal view.
    expect(inventoryTerminals([cursorPane, bash], true)).toEqual([bash]);
  });

  it("drops the kiro vendor pane for native Kiro sessions", () => {
    // Regression: terminal_kiro_main was missing from AGENT_TERMINAL_IDS,
    // so Kiro's Terminal pill opened a shell view with an X close button
    // instead of preserving the Chat/Terminal toggle.
    expect(inventoryTerminals([kiroPane, bash], true)).toEqual([bash]);
  });

  it("drops the goose vendor pane for native Goose sessions", () => {
    // Regression: terminal_goose_main was missing from AGENT_TERMINAL_IDS, so
    // the goose TUI pane leaked into the Shells inventory and (via isShellView)
    // opened as a plain shell while hiding the Chat/Terminal pill — same failure
    // mode as the pi/cursor panes above.
    expect(inventoryTerminals([goosePane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_goose_main")).toBe(true);
  });

  it("drops the qwen vendor pane for native Qwen sessions", () => {
    // Regression: terminal_qwen_main was missing from AGENT_TERMINAL_IDS, so
    // clicking Terminal opened the qwen TUI pane as a plain shell (shell-header
    // chrome) while hiding the Chat/Terminal pill via isShellView — same failure
    // mode as the pi/cursor/goose panes above.
    expect(inventoryTerminals([qwenPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_qwen_main")).toBe(true);
  });

  it("drops the kimi vendor pane for native Kimi sessions", () => {
    // Regression: terminal_kimi_main was missing from AGENT_TERMINAL_IDS,
    // same failure mode as the pi/cursor panes above — leaked into Shells
    // and hid the Chat/Terminal pill in Terminal view.
    expect(inventoryTerminals([kimiPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_kimi_main")).toBe(true);
  });

  it("drops the hermes vendor pane for native Hermes sessions", () => {
    // Regression: terminal_hermes_main was missing from AGENT_TERMINAL_IDS, so
    // the hermes TUI pane leaked into the Shells inventory and (via isShellView)
    // opened as a plain shell while hiding the Chat/Terminal pill — same failure
    // mode as the pi/cursor/goose/qwen panes above.
    expect(inventoryTerminals([hermesPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_hermes_main")).toBe(true);
  });

  it("drops the antigravity vendor pane for native Antigravity sessions", () => {
    // Regression (#1157): terminal_antigravity_main was missing from
    // AGENT_TERMINAL_IDS, so the agy TUI pane leaked into the Shells inventory
    // and (via isShellView) hid the Chat/Terminal pill in Terminal view —
    // stranding the user with no way back to Chat. Same failure mode as the
    // pi/cursor/goose/qwen panes above.
    expect(inventoryTerminals([antigravityPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_antigravity_main")).toBe(true);
  });

  it("drops the embedded REPL terminal for terminal-first SDK sessions", () => {
    // The REPL terminal backs the pill's Terminal view; listing it in
    // the rail reads as a phantom "main" terminal on agents that don't
    // run a TUI. Agent-launched terminals must survive the filter — an
    // empty result here would hide the agent's real terminals too.
    expect(inventoryTerminals([repl, bash], true)).toEqual([bash]);
  });

  it("drops the vendor pane for native-wrapper sessions", () => {
    // Native sessions share the SDK rule: the claude/codex pane IS the
    // pill's Terminal view, so the Shells rail must list only user
    // shells. The pane surviving the filter re-creates the phantom
    // "main" entry on claude code / codex sessions.
    expect(inventoryTerminals([claudePane, bash], true)).toEqual([bash]);
  });

  it("hides the shell list entirely when the agent terminal is the only one", () => {
    // [] (not [repl]) is what makes the rail show the virtual
    // new-shell row alone for sessions with no user shells.
    expect(inventoryTerminals([repl], true)).toEqual([]);
  });

  it("keeps the full list for non-terminal-first sessions", () => {
    // A regular session never hosts an agent terminal; an id collision
    // (agent-declared "tui"/"main") must not be hidden.
    expect(inventoryTerminals([repl, bash], false)).toEqual([repl, bash]);
  });
});

describe("isAgentTerminalKey", () => {
  it("recognizes the agent's own terminal for every session shape", () => {
    expect(isAgentTerminalKey("terminal:terminal_tui_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_claude_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_codex_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_opencode_main")).toBe(true);
    // pi-native: missing here is what hid the Chat/Terminal pill in
    // Terminal view (isShellView wrongly true) for Pi sessions.
    expect(isAgentTerminalKey("terminal:terminal_pi_main")).toBe(true);
    // cursor-native: same regression class as pi above.
    expect(isAgentTerminalKey("terminal:terminal_cursor_main")).toBe(true);
    // kiro-/goose-/qwen-native: same regression class as cursor/pi above.
    expect(isAgentTerminalKey("terminal:terminal_kiro_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_goose_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_qwen_main")).toBe(true);
  });

  it("treats a user shell as not-the-agent-terminal", () => {
    expect(isAgentTerminalKey("terminal:terminal_bash_s1")).toBe(false);
  });
});

describe("findAgentTerminal", () => {
  it("selects the agent pane even when a user shell appears first", () => {
    const shell: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };
    const agent: TerminalInfo = {
      id: "terminal_codex_main",
      name: "codex",
      session: "main",
      running: true,
    };

    expect(findAgentTerminal([shell, agent])).toBe(agent);
  });

  it("does not substitute a user shell when the agent pane is absent", () => {
    const shell: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };

    expect(findAgentTerminal([shell])).toBeNull();
  });
});
