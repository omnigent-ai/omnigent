import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { noteTerminalInput, resetTerminalInputTimes } from "@/lib/terminalInputTimes";

import {
  checkShellClose,
  SHELL_CLOSE_CHECK_MS,
  joinShellTabsCloses,
  useShellCloseCheck,
  useShellCloseRequest,
  type ShellCloseCheck,
} from "./shellCloseCheck";

const fetchMock = vi.hoisted(() => vi.fn<(url: string, init?: RequestInit) => Promise<Response>>());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answer each terminal's shell route from `answers`, keyed by terminal id. */
function runnerSays(answers: Record<string, Response | (() => Promise<Response>)>) {
  fetchMock.mockImplementation(async (url, init) => {
    const terminalId = decodeURIComponent(url.split("/").at(-2) ?? "");
    const answer = answers[terminalId];
    if (typeof answer === "function") {
      return new Promise<Response>((resolve, reject) => {
        init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted")));
        void answer().then(resolve);
      });
    }
    return answer.clone();
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  resetTerminalInputTimes();
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("checkShellClose", () => {
  it("asks the session's runner about each terminal through the server", async () => {
    runnerSays({ terminal_zsh_s1: json({ state: "idle" }) });
    await checkShellClose("conv a", ["terminal_zsh_s1"]);
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv%20a/resources/terminals/terminal_zsh_s1/shell",
    );
  });

  it.each([
    [{ state: "idle" }, { close: "now" }, "idle"],
    [
      { state: "busy", foreground: "vim", background_jobs: 1 },
      { close: "confirm", busy: { foreground: "vim", backgroundJobs: 1 } },
      "busy",
    ],
    [
      { state: "busy", foreground: null, background_jobs: null },
      { close: "confirm", busy: { foreground: null, backgroundJobs: null } },
      "busy",
    ],
    [{ state: "unknown" }, { close: "confirm", busy: null }, "unknown"],
    [{ state: "sleeping" }, { close: "confirm", busy: null }, "unknown"],
  ])("answers %j with %j", async (body, check, reason) => {
    runnerSays({ terminal_zsh_s1: json(body) });
    await expect(checkShellClose("conv_a", ["terminal_zsh_s1"])).resolves.toEqual({
      check,
      reason,
    });
  });

  it.each([
    [404, "unsupported"],
    [405, "unsupported"],
    [503, "unsupported"],
    [502, "error"],
    [403, "error"],
  ])("asks when the server answers %i", async (status, reason) => {
    runnerSays({ terminal_zsh_s1: json({ error: {} }, status) });
    await expect(checkShellClose("conv_a", ["terminal_zsh_s1"])).resolves.toEqual({
      check: { close: "confirm", busy: null },
      reason,
    });
  });

  it("asks when the request fails", async () => {
    fetchMock.mockRejectedValue(new TypeError("offline"));
    await expect(checkShellClose("conv_a", ["terminal_zsh_s1"])).resolves.toMatchObject({
      reason: "error",
    });
  });

  it("keeps only a short printable command name", async () => {
    runnerSays({
      terminal_a: json({
        state: "busy",
        foreground: "\ufeffv\u200bi\u202em\u0007",
        background_jobs: 0,
      }),
      terminal_b: json({ state: "busy", foreground: "x".repeat(40), background_jobs: -2 }),
    });
    const named = await checkShellClose("conv_a", ["terminal_a"]);
    expect(named.check).toEqual({
      close: "confirm",
      busy: { foreground: "vim", backgroundJobs: 0 },
    });
    const unnamed = await checkShellClose("conv_a", ["terminal_b"]);
    expect(unnamed.check).toEqual({
      close: "confirm",
      busy: { foreground: null, backgroundJobs: null },
    });
  });

  it("closes several shells without asking only when every one is idle", async () => {
    runnerSays({ terminal_a: json({ state: "idle" }), terminal_b: json({ state: "idle" }) });
    await expect(checkShellClose("conv_a", ["terminal_a", "terminal_b"])).resolves.toMatchObject({
      check: { close: "now" },
    });
    runnerSays({
      terminal_a: json({ state: "idle" }),
      terminal_b: json({ state: "busy", foreground: "vim", background_jobs: 0 }),
    });
    await expect(checkShellClose("conv_a", ["terminal_a", "terminal_b"])).resolves.toEqual({
      check: { close: "confirm", busy: null },
      reason: "busy",
    });
  });

  it("asks when the answer misses its budget", async () => {
    vi.useFakeTimers();
    runnerSays({ terminal_zsh_s1: () => new Promise<Response>(() => {}) });
    const checking = checkShellClose("conv_a", ["terminal_zsh_s1"]);
    await vi.advanceTimersByTimeAsync(SHELL_CLOSE_CHECK_MS + 1);
    await expect(checking).resolves.toEqual({
      check: { close: "confirm", busy: null },
      reason: "timeout",
    });
  });

  it("asks within its budget even when a read ignores its signal", async () => {
    vi.useFakeTimers();
    // Stalled before the request goes out, as a slow host lookup would be.
    fetchMock.mockImplementation(() => new Promise<Response>(() => {}));
    const checking = checkShellClose("conv_a", ["terminal_zsh_s1"]);
    await vi.advanceTimersByTimeAsync(SHELL_CLOSE_CHECK_MS + 1);
    await expect(checking).resolves.toEqual({
      check: { close: "confirm", busy: null },
      reason: "timeout",
    });
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
  });

  it("asks when the user typed into the shell while it was being read", async () => {
    let answer!: (response: Response) => void;
    runnerSays({
      terminal_zsh_s1: () =>
        new Promise<Response>((resolve) => {
          answer = resolve;
        }),
    });
    const checking = checkShellClose("conv_a", ["terminal_zsh_s1"]);
    await Promise.resolve();
    noteTerminalInput("conv_a", "terminal_zsh_s1");
    answer(json({ state: "idle" }));
    await expect(checking).resolves.toEqual({
      check: { close: "confirm", busy: null },
      reason: "typed",
    });
  });
});

describe("useShellCloseCheck", () => {
  it("records why a close skipped the question, naming the close path", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    runnerSays({ terminal_zsh_s1: json({ state: "idle" }) });
    const decide = vi.fn<(check: ShellCloseCheck) => void>();
    const { result } = renderHook(() => useShellCloseCheck("conv_a"));
    await act(async () => {
      result.current(["terminal_zsh_s1"], "pane_close", decide);
      await vi.waitFor(() => expect(decide).toHaveBeenCalled());
    });
    expect(decide).toHaveBeenCalledExactlyOnceWith({ close: "now" });
    expect(info).toHaveBeenCalledWith("[omnigent] shell close check", {
      event: "shell_close_check",
      trigger: "pane_close",
      decision: "closed_without_asking",
      reason: "idle",
      terminalIds: ["terminal_zsh_s1"],
    });
  });

  it("asks at once, without a request, when there's no session", () => {
    const decide = vi.fn<(check: ShellCloseCheck) => void>();
    const { result } = renderHook(() => useShellCloseCheck(null));
    result.current(["terminal_zsh_s1"], "pane_close", decide);
    expect(decide).toHaveBeenCalledExactlyOnceWith({ close: "confirm", busy: null });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("drops a check that's still running when a newer one begins or the session changes", async () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    const answers: ((response: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          answers.push(resolve);
        }),
    );
    const stale = vi.fn<(check: ShellCloseCheck) => void>();
    const newer = vi.fn<(check: ShellCloseCheck) => void>();
    const { result, rerender } = renderHook(({ session }) => useShellCloseCheck(session), {
      initialProps: { session: "conv_a" },
    });
    const staleDropped = vi.fn();
    result.current(["terminal_zsh_s1"], "pane_close", stale, staleDropped);
    result.current(["terminal_zsh_s1"], "pane_close", newer);
    await vi.waitFor(() => expect(answers).toHaveLength(2));
    // The older read stops; a newer close answers for itself, so the older says nothing.
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    answers[0](json({ state: "idle" }));
    answers[1](json({ state: "idle" }));
    await vi.waitFor(() => expect(newer).toHaveBeenCalled());
    expect(stale).not.toHaveBeenCalled();
    expect(staleDropped).not.toHaveBeenCalled();

    const otherSession = vi.fn<(check: ShellCloseCheck) => void>();
    const otherSessionDropped = vi.fn();
    result.current(["terminal_zsh_s1"], "pane_close", otherSession, otherSessionDropped);
    await vi.waitFor(() => expect(answers).toHaveLength(3));
    rerender({ session: "conv_b" });
    answers[2](json({ state: "idle" }));
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    expect(otherSession).not.toHaveBeenCalled();
    // The surface now shows another session, so the close neither happens nor asks.
    expect(otherSessionDropped).toHaveBeenCalledOnce();
  });
});

describe("useShellCloseRequest", () => {
  it("closes an idle shell at once, and holds a busy one for the dialog", async () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));

    runnerSays({ terminal_zsh_s1: json({ state: "idle" }) });
    act(() => result.current.request("tab-1", ["terminal_zsh_s1"]));
    await vi.waitFor(() => expect(close).toHaveBeenCalledExactlyOnceWith(["tab-1"]));
    expect(result.current.pending).toBeNull();

    runnerSays({ terminal_zsh_s1: json({ state: "busy", foreground: "top", background_jobs: 0 }) });
    act(() => result.current.request("tab-1", ["terminal_zsh_s1"]));
    await vi.waitFor(() =>
      expect(result.current.pending).toEqual({
        items: ["tab-1"],
        busy: { foreground: "top", backgroundJobs: 0 },
      }),
    );
    act(() => result.current.confirm());
    expect(close).toHaveBeenCalledTimes(2);
    expect(result.current.pending).toBeNull();
  });

  it("asks at once for a shell it can't name", () => {
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    act(() => result.current.request("tab-1", null));
    expect(result.current.pending).toEqual({ items: ["tab-1"], busy: null });
    act(() => result.current.cancel());
    expect(close).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("joins a close requested while another is checking, closing both when both are idle", async () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    let answer = () => {};
    const held = new Promise<void>((resolve) => {
      answer = resolve;
    });
    runnerSays({
      terminal_one: async () => {
        await held;
        return json({ state: "idle" });
      },
      terminal_two: json({ state: "idle" }),
    });
    act(() => result.current.request("tab-1", ["terminal_one"]));
    act(() => result.current.request("tab-2", ["terminal_two"]));
    act(() => answer());
    await vi.waitFor(() => expect(close).toHaveBeenCalledExactlyOnceWith(["tab-1", "tab-2"]));
  });

  it("asks about a joined close when the earlier shell was typed into before the join", async () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    let clock = 0;
    vi.spyOn(performance, "now").mockImplementation(() => (clock += 10));
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    let answer = () => {};
    const held = new Promise<void>((resolve) => {
      answer = resolve;
    });
    runnerSays({
      terminal_one: async () => {
        await held;
        return json({ state: "idle" });
      },
      terminal_two: json({ state: "idle" }),
    });
    act(() => result.current.request("tab-1", ["terminal_one"]));
    noteTerminalInput("conv_a", "terminal_one");
    act(() => result.current.request("tab-2", ["terminal_two"]));
    act(() => answer());
    // Both read idle, but the first shell took input after its close was asked.
    await vi.waitFor(() =>
      expect(result.current.pending).toEqual({ items: ["tab-1", "tab-2"], busy: null }),
    );
    expect(close).not.toHaveBeenCalled();
  });

  it("asks once about both when the joined close finds one busy", async () => {
    vi.spyOn(console, "info").mockImplementation(() => {});
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    runnerSays({
      terminal_one: json({ state: "idle" }),
      terminal_two: json({ state: "busy", foreground: "vim", background_jobs: 0 }),
    });
    act(() => {
      result.current.request("tab-1", ["terminal_one"]);
      result.current.request("tab-2", ["terminal_two"]);
    });
    await vi.waitFor(() =>
      expect(result.current.pending).toEqual({ items: ["tab-1", "tab-2"], busy: null }),
    );
    expect(close).not.toHaveBeenCalled();
  });

  it("asks about a close it can't check together with one still checking", () => {
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    runnerSays({ terminal_one: async () => new Promise<Response>(() => {}) });
    act(() => result.current.request("tab-1", ["terminal_one"]));
    act(() => result.current.request("tab-2", null));
    expect(result.current.pending).toEqual({ items: ["tab-1", "tab-2"], busy: null });
  });

  it("drops its question, and what it was checking, when the surface shows another session", async () => {
    const close = vi.fn<(items: readonly string[]) => void>();
    const { result, rerender } = renderHook(
      ({ sessionId }) => useShellCloseRequest(sessionId, "grid_close", close),
      { initialProps: { sessionId: "conv_a" } },
    );
    act(() => result.current.request("tab-1", null));
    rerender({ sessionId: "conv_b" });
    expect(result.current.pending).toBeNull();
    // Back on the first session, the question is gone, and confirming closes nothing.
    rerender({ sessionId: "conv_a" });
    expect(result.current.pending).toBeNull();
    act(() => result.current.confirm());
    expect(close).not.toHaveBeenCalled();

    runnerSays({ terminal_one: async () => new Promise<Response>(() => {}) });
    act(() => result.current.request("tab-b", ["terminal_one"]));
    rerender({ sessionId: "conv_c" });
    runnerSays({ terminal_two: json({ state: "idle" }) });
    vi.spyOn(console, "info").mockImplementation(() => {});
    act(() => result.current.request("tab-c", ["terminal_two"]));
    await vi.waitFor(() => expect(close).toHaveBeenCalledExactlyOnceWith(["tab-c"]));
  });
});

describe("joinShellTabsCloses", () => {
  it("closes every tab once, telling each caller about its own", () => {
    const first = { onConfirmed: vi.fn(), onFailed: vi.fn() };
    const second = { onConfirmed: vi.fn(), onFailed: vi.fn() };
    const joined = joinShellTabsCloses([
      { keys: ["a", "b"], callbacks: first },
      { keys: ["b", "c"], callbacks: second },
      { keys: ["d"] },
    ]);
    expect(joined.keys).toEqual(["a", "b", "c", "d"]);
    joined.callbacks.onConfirmed?.();
    expect(first.onConfirmed).toHaveBeenCalledTimes(1);
    expect(second.onConfirmed).toHaveBeenCalledTimes(1);
    joined.callbacks.onFailed?.("a");
    expect(first.onFailed).toHaveBeenCalledExactlyOnceWith("a");
    expect(second.onFailed).not.toHaveBeenCalled();
  });
});
