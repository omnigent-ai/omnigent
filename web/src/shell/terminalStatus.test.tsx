import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { type ConnectionState } from "@/components/blocks/TerminalSession";
import { type TerminalInfo } from "@/hooks/useTerminals";
import { deriveTerminalStatus, TerminalStatusBadge } from "./terminalStatus";

afterEach(() => {
  cleanup();
});

function terminal(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_bash_s1",
    name: "bash",
    session: "s1",
    running: true,
    ...overrides,
  };
}

describe("deriveTerminalStatus", () => {
  it("shows an attached running terminal as idle without using global conversation state", () => {
    expect(deriveTerminalStatus(terminal({ running: true }), null)).toBe("idle");
  });

  it("shows recent terminal output as active", () => {
    expect(deriveTerminalStatus(terminal({ running: true }), { kind: "connected" }, true)).toBe(
      "active",
    );
  });

  it("falls back to resource state when connection state is null (unmounted terminal)", () => {
    // null connection state means the TerminalView is not mounted — status
    // must be derived from the resource alone, not a stale bridge state.
    expect(deriveTerminalStatus(terminal({ running: true }), null)).toBe("idle");
    expect(deriveTerminalStatus(terminal({ running: false }), null)).toBe("closed");
  });

  it("shows a stopped terminal resource as closed", () => {
    expect(deriveTerminalStatus(terminal({ running: false }), null)).toBe("closed");
  });

  it.each([
    [{ kind: "connecting" }, "connecting"],
    [{ kind: "error" }, "error"],
    [{ kind: "closed", reason: "code 1006", code: 1006 }, "closed"],
  ] satisfies [ConnectionState, string][])(
    "lets the active bridge state override the terminal resource state",
    (connectionState, expected) => {
      expect(deriveTerminalStatus(terminal({ running: true }), connectionState, true)).toBe(
        expected,
      );
    },
  );
});

describe("TerminalStatusBadge", () => {
  it("renders a visible idle label for terminal selectors", () => {
    render(<TerminalStatusBadge status="idle" />);

    expect(screen.getByText("Idle")).toBeInTheDocument();
    expect(screen.getByLabelText("Idle")).toBeInTheDocument();
  });

  it("keeps dot size consistent while using per-status colors", () => {
    const { rerender } = render(<TerminalStatusBadge status="active" />);

    let dot = screen.getByLabelText("Active").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-emerald-500");
    expect(dot).not.toHaveClass("animate-pulse");

    rerender(<TerminalStatusBadge status="closed" />);
    dot = screen.getByLabelText("Closed").querySelector("span");
    expect(dot).toHaveClass("size-1.5", "bg-black", "dark:bg-white");
  });
});
