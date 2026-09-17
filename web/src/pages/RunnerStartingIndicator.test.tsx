import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerStartingIndicator } from "./ChatIndicators";
import { useChatStore } from "@/store/chatStore";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";

/**
 * Build a TerminalFirstContextValue with sensible defaults so each test
 * overrides only the fields it exercises. `terminalStartingUp` is the only
 * field this component reads — it defaults to false (steady state).
 */
function makeCtx(overrides: Partial<TerminalFirstContextValue> = {}): TerminalFirstContextValue {
  return {
    isClaudeNative: true,
    isNativeWrapper: true,
    isTerminalFirst: true,
    isShellView: false,
    view: "chat",
    terminalViewKey: null,
    setView: vi.fn(),
    terminalsAvailable: false,
    terminalStartingUp: false,
    ...overrides,
  };
}

/**
 * Render RunnerStartingIndicator under a TerminalFirst context (or none, to
 * model a non-terminal-first session where `useTerminalFirst()` is null).
 */
function renderWithContext(variant: "hero" | "row", ctx: TerminalFirstContextValue | null) {
  return render(
    ctx ? (
      <TerminalFirstContextProvider value={ctx}>
        <RunnerStartingIndicator variant={variant} />
      </TerminalFirstContextProvider>
    ) : (
      <RunnerStartingIndicator variant={variant} />
    ),
  );
}

afterEach(() => {
  cleanup();
  // The component reads sandboxStatus from the module-scoped store —
  // reset it so a stage set in one test can't leak into the next.
  useChatStore.setState({ sandboxStatus: null });
});

describe("RunnerStartingIndicator", () => {
  it.each(["hero", "row"] as const)(
    "%s: renders nothing for ordinary terminal startup",
    (variant) => {
      const { container } = renderWithContext(variant, makeCtx({ terminalStartingUp: true }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
      expect(container).toBeEmptyDOMElement();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: renders nothing once the terminal is available (spin-up finished)",
    (variant) => {
      // terminalsAvailable true ⇒ AppShell keeps terminalStartingUp false: a
      // reachable PTY is never "loading", so the placeholder must clear.
      const { container } = renderWithContext(
        variant,
        makeCtx({ terminalsAvailable: true, terminalStartingUp: false }),
      );
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
      expect(container).toBeEmptyDOMElement();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: renders nothing for a non-terminal-first session (no terminal context)",
    (variant) => {
      // A regular agent (e.g. nessie) gets the generic ConnectionIndicator
      // "Connecting…" band instead — this main-pane cue is terminal-first
      // only, so with no TerminalFirst context it must no-op.
      const { container } = renderWithContext(variant, null);
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
      expect(container).toBeEmptyDOMElement();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: renders nothing for a non-terminal-first session that is spinning up",
    (variant) => {
      // Real-world shape: the TerminalFirst provider is always mounted, so a
      // regular agent (e.g. nessie) has isTerminalFirst:false — and AppShell
      // still computes terminalStartingUp:true for it during cold launch. This
      // indicator gates on isTerminalFirst (nessie gets the generic
      // ConnectionIndicator band instead), so it must NOT render here.
      const { container } = renderWithContext(
        variant,
        makeCtx({ isTerminalFirst: false, terminalStartingUp: true }),
      );
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
      expect(container).toBeEmptyDOMElement();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: shows the sandbox stage label during a managed launch, for any session type",
    (variant) => {
      // Sandbox launches report stages for ALL session types — even a
      // non-terminal-first session with no spin-up renders the stage.
      useChatStore.setState({ sandboxStatus: { stage: "provisioning", error: null } });
      renderWithContext(variant, makeCtx({ isTerminalFirst: false, terminalStartingUp: false }));
      const indicator = screen.getByTestId("runner-starting-indicator");
      // The stage copy is the user-facing contract; a regression here
      // reverts sandbox sessions to a silent dead chat during launch.
      expect(indicator).toHaveTextContent(/provisioning sandbox/i);
      expect(indicator.querySelector(".animate-spin")).not.toBeNull();
    },
  );

  it("row: sandbox stage still renders during terminal startup", () => {
    useChatStore.setState({ sandboxStatus: { stage: "cloning", error: null } });
    renderWithContext("row", makeCtx({ terminalStartingUp: true }));
    const indicator = screen.getByTestId("runner-starting-indicator");
    expect(indicator).toHaveTextContent(/cloning repository/i);
    expect(indicator).not.toHaveTextContent(/starting up/i);
  });

  it.each(["hero", "row"] as const)(
    "%s: renders nothing for a FAILED sandbox launch",
    (variant) => {
      // Failure belongs to the destructive SandboxFailedIndicator band —
      // rendering a spinner here would read as "still launching".
      useChatStore.setState({
        sandboxStatus: { stage: "failed", error: "managed sandbox launch failed: boom" },
      });
      const { container } = renderWithContext(
        variant,
        makeCtx({ isTerminalFirst: false, terminalStartingUp: false }),
      );
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
      expect(container).toBeEmptyDOMElement();
    },
  );
});
