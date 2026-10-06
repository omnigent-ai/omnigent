import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RunnerStartingIndicator } from "./ChatIndicators";
import { useChatStore } from "@/store/chatStore";
import {
  TerminalFirstContextProvider,
  type TerminalFirstContextValue,
} from "@/shell/TerminalFirstContext";

/** A native session whose terminal is absent, with no unrelated startup signal. */
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
  useChatStore.setState({
    sandboxStatus: null,
    sessionConfigPhase: null,
    sessionConfigError: null,
    pendingModelChange: null,
  });
});

describe("RunnerStartingIndicator", () => {
  it.each(["hero", "row"] as const)(
    "%s: shows explicit recovery without a terminal context",
    (variant) => {
      useChatStore.setState({ sessionConfigPhase: "starting" });
      renderWithContext(variant, null);

      const indicator = screen.getByTestId("runner-starting-indicator");
      expect(indicator).toHaveTextContent("Starting up…");
      expect(indicator).toHaveAttribute("role", "status");
      expect(indicator.querySelector(".animate-spin")).not.toBeNull();

      act(() => useChatStore.setState({ sessionConfigPhase: "applying" }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();

      act(() => useChatStore.setState({ sessionConfigPhase: null }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: keeps native startup visible through application and model confirmation",
    (variant) => {
      useChatStore.setState({ sessionConfigPhase: "starting" });
      renderWithContext(variant, makeCtx());
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");

      act(() => useChatStore.setState({ sessionConfigPhase: "applying" }));
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");

      act(() => useChatStore.setState({ sessionConfigPhase: null, pendingModelChange: "sonnet" }));
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");

      act(() => useChatStore.setState({ pendingModelChange: null }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: clears startup when the native terminal appears before configuration settles",
    (variant) => {
      useChatStore.setState({ sessionConfigPhase: "applying" });
      const ctx = makeCtx();
      const { rerender } = renderWithContext(variant, ctx);
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");

      const tree = (terminalsAvailable: boolean) => (
        <TerminalFirstContextProvider value={{ ...ctx, terminalsAvailable }}>
          <RunnerStartingIndicator variant={variant} />
        </TerminalFirstContextProvider>
      );
      rerender(tree(true));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();

      act(() => useChatStore.setState({ sessionConfigPhase: null, pendingModelChange: "sonnet" }));
      rerender(tree(false));
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");
      rerender(tree(true));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: does not treat SDK configuration as native terminal startup",
    (variant) => {
      useChatStore.setState({ sessionConfigPhase: "applying" });
      renderWithContext(variant, makeCtx({ isClaudeNative: false, isNativeWrapper: false }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();

      act(() => useChatStore.setState({ sessionConfigPhase: null, pendingModelChange: "sonnet" }));
      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    },
  );

  it.each(["hero", "row"] as const)(
    "%s: clears model/effort startup when recovery fails",
    (variant) => {
      useChatStore.setState({ sessionConfigPhase: "starting" });
      renderWithContext(variant, null);
      expect(screen.getByTestId("runner-starting-indicator")).toHaveTextContent("Starting up…");

      act(() =>
        useChatStore.setState({
          sessionConfigPhase: null,
          sessionConfigError: "Terminal failed to start",
        }),
      );

      expect(screen.queryByTestId("runner-starting-indicator")).toBeNull();
    },
  );

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
    "%s: prefers sandbox progress while model/effort recovery is starting",
    (variant) => {
      useChatStore.setState({
        sandboxStatus: { stage: "cloning", error: null },
        sessionConfigPhase: "starting",
      });
      renderWithContext(variant, null);

      const indicator = screen.getByTestId("runner-starting-indicator");
      expect(indicator).toHaveTextContent(/cloning repository/i);
      expect(indicator).not.toHaveTextContent(/starting up/i);
    },
  );

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
