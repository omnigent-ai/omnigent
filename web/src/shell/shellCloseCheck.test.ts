import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { noteTerminalInput, resetTerminalInputTimes } from "@/lib/terminalInputTimes";

import {
  checkShellClose,
  SHELL_CLOSE_CHECK_MS,
  useShellCloseCheck,
  useShellCloseRequest,
  type ShellCloseCheck,
} from "./shellCloseCheck";

const fetchMock = vi.hoisted(() => vi.fn<(url: string, init?: RequestInit) => Promise<Response>>());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: fetchMock }));
// Only floating panes close an idle shell without asking.
const floating = vi.hoisted(() => ({ on: true }));
vi.mock("./useFloatingPanes", () => ({ useFloatingPanes: () => floating.on }));

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
  floating.on = true;
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

  it("asks at once, reading nothing and recording nothing, without floating panes", () => {
    floating.on = false;
    runnerSays({ terminal_zsh_s1: json({ state: "idle" }) });
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const decide = vi.fn<(check: ShellCloseCheck) => void>();
    const { result } = renderHook(() => useShellCloseCheck("conv_a"));
    result.current(["terminal_zsh_s1"], "pane_close", decide);
    expect(decide).toHaveBeenCalledExactlyOnceWith({ close: "confirm", busy: null });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(info).not.toHaveBeenCalled();
  });

  it("asks at once, without a request, when there's no session", () => {
    const decide = vi.fn<(check: ShellCloseCheck) => void>();
    const { result } = renderHook(() => useShellCloseCheck(null));
    result.current(["terminal_zsh_s1"], "pane_close", decide);
    expect(decide).toHaveBeenCalledExactlyOnceWith({ close: "confirm", busy: null });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("asks instead when floating panes turn off while a check is still running", async () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => {});
    const answers: ((response: Response) => void)[] = [];
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          answers.push(resolve);
        }),
    );
    const decide = vi.fn<(check: ShellCloseCheck) => void>();
    const { result, rerender } = renderHook(() => useShellCloseCheck("conv_a"));
    result.current(["terminal_zsh_s1"], "pane_close", decide);
    await vi.waitFor(() => expect(answers).toHaveLength(1));
    floating.on = false;
    rerender();
    answers[0](json({ state: "idle" }));
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });
    // The close asks, as every close does with the gate off; the idle answer arrives too late.
    expect(decide).toHaveBeenCalledExactlyOnceWith({ close: "confirm", busy: null });
    expect(info).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls[0][1]?.signal?.aborted).toBe(true);
    // The next close, with the gate off, asks at once.
    result.current(["terminal_zsh_s1"], "pane_close", decide);
    expect(decide).toHaveBeenCalledTimes(2);
    expect(decide).toHaveBeenLastCalledWith({ close: "confirm", busy: null });
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
    const close = vi.fn<(key: string) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));

    runnerSays({ terminal_zsh_s1: json({ state: "idle" }) });
    act(() => result.current.request("tab-1", ["terminal_zsh_s1"]));
    await vi.waitFor(() => expect(close).toHaveBeenCalledExactlyOnceWith("tab-1"));
    expect(result.current.pending).toBeNull();

    runnerSays({ terminal_zsh_s1: json({ state: "busy", foreground: "top", background_jobs: 0 }) });
    act(() => result.current.request("tab-1", ["terminal_zsh_s1"]));
    await vi.waitFor(() =>
      expect(result.current.pending).toEqual({
        item: "tab-1",
        busy: { foreground: "top", backgroundJobs: 0 },
      }),
    );
    act(() => result.current.confirm());
    expect(close).toHaveBeenCalledTimes(2);
    expect(result.current.pending).toBeNull();
  });

  it("asks at once for a shell it can't name", () => {
    const close = vi.fn<(key: string) => void>();
    const { result } = renderHook(() => useShellCloseRequest("conv_a", "grid_close", close));
    act(() => result.current.request("tab-1", null));
    expect(result.current.pending).toEqual({ item: "tab-1", busy: null });
    act(() => result.current.cancel());
    expect(close).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
