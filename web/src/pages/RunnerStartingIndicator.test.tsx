import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerStartingIndicator } from "./ChatPage";
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
  it("hero: shows a spinner + Starting up… copy while a terminal-first session is spinning up", () => {
    renderWithContext("hero", makeCtx({ terminalStartingUp: true }));
    const indicator = screen.getByTestId("runner-starting-indicator");
    expect(indicator).toBeInTheDocument();
    // The exact copy is the user-facing contract — assert the value, not just
    // that *something* rendered (a null/empty state would also pass length>=1).
    expect(indicator).toHaveTextContent(/starting up/i);
    // The animated spinner is what makes "work is happening" obvious.
    expect(indicator.querySelector(".animate-spin")).not.toBeNull();
    // Announced to assistive tech as a transient status, not a static region.
    expect(indicator).toHaveAttribute("role", "status");
    expect(indicator).toHaveAttribute("aria-live", "polite");
  });

  it("row: shows the in-thread spinner + Starting up… copy while spinning up", () => {
    // The create-then-send path renders the user bubble immediately, so the
    // cue has to sit *in* the thread beneath it rather than as an empty state.
    renderWithContext("row", makeCtx({ terminalStartingUp: true }));
    const indicator = screen.getByTestId("runner-starting-indicator");
    expect(indicator).toBeInTheDocument();
    expect(indicator).toHaveTextContent(/starting up/i);
    expect(indicator.querySelector(".animate-spin")).not.toBeNull();
    expect(indicator).toHaveAttribute("role", "status");
    expect(indicator).toHaveAttribute("aria-live", "polite");
  });

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

  it("row: sandbox stage label wins over the terminal Starting up… copy", () => {
    // Both launch shapes active at once (terminal-first session in a
    // sandbox): the stage is strictly more specific, so it must show
    // INSTEAD of the generic terminal copy — not alongside it.
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
