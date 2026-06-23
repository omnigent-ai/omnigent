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
  fetchTerminals,
  inventoryTerminals,
  isAgentTerminalKey,
  PENDING_RECONCILE_INTERVAL_MS,
  terminalInfoFromResource,
  terminalsReconcileInterval,
  terminalTabKey,
  useTerminals,
  type TerminalInfo,
} from "./useTerminals";

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

  it("returns [] for a not-yet-reachable runner (404/409/502/503)", async () => {
    // These are "no terminal yet", not errors: the live SSE event fills
    // the rail once the runner binds, so the seed must not throw.
    for (const status of [404, 409, 502, 503]) {
      fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status }));
      expect(await fetchTerminals("conv_abc")).toEqual([]);
    }
  });

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

describe("terminalsReconcileInterval", () => {
  // The spinner is `terminalPending && !terminalsAvailable`. This decides
  // when the terminals query re-polls to recover a missed
  // `session.resource.created` (the dbx-apps stuck-spinner bug) — and,
  // critically, when it STOPS so there's no steady-state polling.
  it("polls only while pending AND no terminal is visible yet", () => {
    expect(terminalsReconcileInterval(true, 0)).toBe(PENDING_RECONCILE_INTERVAL_MS);
  });

  it("stops the instant a terminal lands (clears the spinner via AND)", () => {
    expect(terminalsReconcileInterval(true, 1)).toBe(false);
  });

  it("never polls when the runner is not spinning up a terminal", () => {
    expect(terminalsReconcileInterval(false, 0)).toBe(false);
    expect(terminalsReconcileInterval(false, 2)).toBe(false);
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

  it("re-polls while pending until the terminal lands, then stops", async () => {
    // Mount + first reconcile poll see no terminal; the auto-created
    // terminal lands on the third fetch — exactly the missed-delta case
    // a page refresh used to be the only recovery for.
    fetchMock
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValue(oneTerminal());

    const { result } = renderHook(() => useTerminals("conv_abc", { reconcileWhilePending: true }), {
      wrapper: makeWrapper(),
    });

    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(result.current.terminals).toEqual([]);
    const seedCalls = fetchMock.mock.calls.length;

    // First reconcile poll fires while still empty: the interval scheduled a
    // refetch, so the fetch count must climb past the seed. (Asserting `>`
    // rather than an exact `+1` because React Query anchors the next interval
    // off each fetch's settle time, so the precise count under fake timers is
    // a scheduling detail; a count still == seedCalls would mean no poll fired
    // — the bug we're guarding against.)
    await act(async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS)));
    expect(result.current.terminals).toEqual([]);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(seedCalls);

    // Further reconcile polls -> terminal lands. Assert the full mapped row
    // (not just id) so a broken resource→TerminalInfo mapping can't pass.
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 2)),
    );
    expect(result.current.terminals).toEqual([
      { id: "terminal_claude_main", name: "claude", session: "main", running: true },
    ]);

    // Polling must stop now that a terminal is visible: the refetchInterval
    // returns false once terminalCount > 0. Assert ZERO further fetches over
    // 4 would-be intervals — a higher count would mean the interval was never
    // disabled and the poll runs forever.
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
  const goosePane: TerminalInfo = {
    id: "terminal_goose_main",
    name: "goose",
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

  it("drops the goose vendor pane for native Goose sessions", () => {
    // Regression: terminal_goose_main was missing from AGENT_TERMINAL_IDS, so
    // the goose TUI pane leaked into the Shells inventory and (via isShellView)
    // opened as a plain shell while hiding the Chat/Terminal pill — same failure
    // mode as the pi/cursor panes above.
    expect(inventoryTerminals([goosePane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_goose_main")).toBe(true);
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
    // pi-native: missing here is what hid the Chat/Terminal pill in
    // Terminal view (isShellView wrongly true) for Pi sessions.
    expect(isAgentTerminalKey("terminal:terminal_pi_main")).toBe(true);
    // cursor-native: same regression class as pi above.
    expect(isAgentTerminalKey("terminal:terminal_cursor_main")).toBe(true);
  });

  it("treats a user shell as not-the-agent-terminal", () => {
    expect(isAgentTerminalKey("terminal:terminal_bash_s1")).toBe(false);
  });
});
