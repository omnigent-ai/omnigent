import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo } from "@/hooks/useTerminals";
import { useTerminalStatuses } from "./useTerminalStatuses";

function terminal(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_bash_s1",
    name: "bash",
    session: "s1",
    running: true,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("useTerminalStatuses", () => {
  it("prunes activity state for removed terminals", () => {
    const t1 = terminal({ id: "terminal_bash_s1" });
    const t2 = terminal({ id: "terminal_bash_s2", session: "s2" });
    const { result, rerender } = renderHook(({ terminals }) => useTerminalStatuses(terminals), {
      initialProps: { terminals: [t1, t2] },
    });

    act(() => result.current.markTerminalActive(t2.id));
    expect(result.current.getStatus(t2)).toBe("active");

    rerender({ terminals: [t1] });
    expect(result.current.getStatus(t2)).toBe("idle");
  });

  it("clears connection state when null is passed (terminal unmount)", () => {
    // Simulates a tab switch mid-handshake: the mounted TerminalView fires
    // onStateChange(null) on cleanup, which should remove the stale entry so
    // the selector shows the resource-derived status instead of "connecting".
    const t = terminal();
    const { result } = renderHook(({ terminals }) => useTerminalStatuses(terminals), {
      initialProps: { terminals: [t] },
    });

    act(() => result.current.setTerminalConnectionState(t.id, { kind: "connecting" }));
    expect(result.current.getStatus(t)).toBe("connecting");

    act(() => result.current.setTerminalConnectionState(t.id, null));
    // With no connection state, a running terminal falls back to idle.
    expect(result.current.getStatus(t)).toBe("idle");
  });

  it("keeps connection state updates stable for repeated same-kind events", () => {
    const t = terminal();
    const { result } = renderHook(({ terminals }) => useTerminalStatuses(terminals), {
      initialProps: { terminals: [t] },
    });

    act(() => result.current.setTerminalConnectionState(t.id, { kind: "connected" }));
    const firstGetStatus = result.current.getStatus;

    act(() => result.current.setTerminalConnectionState(t.id, { kind: "connected" }));

    expect(result.current.getStatus).toBe(firstGetStatus);
  });
});
