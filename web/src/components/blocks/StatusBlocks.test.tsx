import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { copyText } from "@/lib/clipboard";
import { ErrorBanner, RoutingDecisionCard } from "./StatusBlocks";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(() => Promise.resolve()) }));

afterEach(cleanup);

const TERMINAL_ERROR = [
  "Required terminal exited unexpectedly; the session runtime is no longer available.",
  "",
  "Terminal diagnostics:",
  "terminal: claude:main",
  "command: claude (8 args; argv omitted because terminal args may contain secrets)",
  "cwd: /Users/corey.zumar",
  "shell: /bin/zsh",
  "pid: 48291",
  "runtime: claude-code 1.0.83",
  "session_id: sess_8f4a2c19",
  "started_at: Tue Aug 11 16:42:18 2026",
  "last_heartbeat: Tue Aug 11 17:00:44 2026",
  "exit_code: 0",
  "signal: none",
  "pty_status: detached",
  "reconnect_attempts: 3",
  "termination_reason: terminal pane no longer available",
  "",
  "Last captured terminal output:",
  "Pane is dead (status 0, Tue Aug 11 17:00:46 2026)",
].join("\n");

describe("ErrorBanner", () => {
  beforeEach(() => vi.mocked(copyText).mockClear());

  it("renders compact by default and expands labeled message content", () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );

    const messageToggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
    expect(messageToggle).toHaveAttribute("aria-expanded", "false");
    expect(messageToggle).not.toHaveAttribute("aria-controls");
    expect(screen.getByTestId("error-headline")).toHaveClass("truncate");
    expect(screen.getByTestId("error-headline")).not.toHaveTextContent("execution");
    expect(screen.queryByText("Message")).toBeNull();
    fireEvent.click(messageToggle);
    expect(messageToggle).toHaveAttribute("aria-expanded", "true");
    expect(messageToggle).toHaveAttribute("aria-controls");
    expect(screen.getByTestId("error-headline")).toHaveClass("truncate");
    expect(screen.getByText("Message")).toBeInTheDocument();
    const message = screen.getByTestId("error-message-content");
    expect(message).toHaveClass("whitespace-pre-wrap");
    expect(message).toHaveClass("break-words");
    expect(message).toHaveTextContent(/Required terminal exited unexpectedly/);
    expect(message).not.toHaveTextContent("terminal: claude:main");
  });

  it("uses one stable leading slot across rest, hover, focus, and expanded states", () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );

    const messageToggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
    expect(screen.getByTestId("error-leading-slot")).toHaveClass("h-[18px]", "w-[18px]");
    expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("error-disclosure-icon")).toBeNull();

    fireEvent.mouseEnter(messageToggle);
    expect(screen.queryByTestId("error-status-icon")).toBeNull();
    expect(screen.getByTestId("error-disclosure-icon")).toHaveClass(
      "text-muted-foreground",
      "group-hover/error:text-foreground",
    );
    expect(screen.getByTestId("error-disclosure-icon")).not.toHaveClass("rotate-90");

    fireEvent.mouseLeave(messageToggle);
    expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("error-disclosure-icon")).toBeNull();

    fireEvent.keyDown(document, { key: "Tab" });
    act(() => messageToggle.focus());
    expect(screen.queryByTestId("error-status-icon")).toBeNull();
    expect(screen.getByTestId("error-disclosure-icon")).not.toHaveClass("rotate-90");

    act(() => messageToggle.blur());
    expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
    fireEvent.click(messageToggle);
    expect(screen.queryByTestId("error-status-icon")).toBeNull();
    expect(screen.getByTestId("error-disclosure-icon")).toHaveClass("rotate-90");
  });

  it("restores the status icon after pointer expand, collapse, and mouse leave", () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );

    const messageToggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
    fireEvent.mouseEnter(messageToggle);
    fireEvent.pointerDown(messageToggle);
    fireEvent.focus(messageToggle);
    fireEvent.pointerUp(messageToggle);
    fireEvent.click(messageToggle);
    expect(screen.queryByTestId("error-status-icon")).toBeNull();
    expect(screen.getByTestId("error-disclosure-icon")).toHaveClass("rotate-90");

    fireEvent.pointerDown(messageToggle);
    fireEvent.pointerUp(messageToggle);
    fireEvent.click(messageToggle);
    expect(screen.queryByTestId("error-status-icon")).toBeNull();
    expect(screen.getByTestId("error-disclosure-icon")).not.toHaveClass("rotate-90");

    fireEvent.mouseLeave(messageToggle);
    expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
    expect(screen.queryByTestId("error-disclosure-icon")).toBeNull();
  });

  it.each([" ", "Enter"])(
    "refreshes keyboard focus visibility after pointer focus and %j activation",
    (key) => {
      render(
        <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
      );

      const messageToggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
      fireEvent.pointerDown(messageToggle);
      act(() => messageToggle.focus());
      fireEvent.pointerUp(messageToggle);
      fireEvent.mouseLeave(messageToggle);
      expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();

      fireEvent.keyDown(messageToggle, { key });
      expect(screen.queryByTestId("error-status-icon")).toBeNull();
      expect(screen.getByTestId("error-disclosure-icon")).not.toHaveClass("rotate-90");
      fireEvent.click(messageToggle);
      expect(screen.getByTestId("error-disclosure-icon")).toHaveClass("rotate-90");

      fireEvent.keyDown(messageToggle, { key });
      fireEvent.click(messageToggle);
      expect(screen.queryByTestId("error-status-icon")).toBeNull();
      expect(screen.getByTestId("error-disclosure-icon")).not.toHaveClass("rotate-90");

      act(() => messageToggle.blur());
      expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
      expect(screen.queryByTestId("error-disclosure-icon")).toBeNull();
    },
  );

  it("replaces the full banner during recovery and restores it after failure", async () => {
    let rejectRetry: ((error: Error) => void) | undefined;
    const onRetry = vi.fn(
      () =>
        new Promise<void>((_resolve, reject) => {
          rejectRetry = reject;
        }),
    );
    render(
      <ErrorBanner
        message={TERMINAL_ERROR}
        source="execution"
        code="required_terminal_exited"
        onRetry={onRetry}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    const reconnecting = screen.getByTestId("error-reconnecting");
    const status = screen.getByRole("status");
    expect(screen.queryByTestId("error-headline")).toBeNull();
    expect(reconnecting).toHaveClass("justify-center", "min-h-14");
    expect(status).toHaveTextContent(/^Reconnecting$/);
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(status).toHaveClass("rounded-xl", "border-border", "bg-background");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss error message" })).toBeNull();
    expect(screen.queryByText("Message")).toBeNull();
    expect(screen.queryByRole("button", { name: "View diagnostics" })).toBeNull();
    expect(screen.queryByTestId("error-status-icon")).toBeNull();

    await act(async () => rejectRetry?.(new Error("Host is still offline")));
    expect(screen.getByTestId("error-headline")).toBeInTheDocument();
    expect(screen.queryByTestId("error-reconnecting")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Retry failed: Host is still offline");
    expect(screen.getByTestId("error-status-icon")).toBeInTheDocument();
  });

  it("matches the prototype pill structure and diagnostics treatment", () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );

    const toggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
    // The expand/collapse control is a real button wrapping icon + headline;
    // the pill container is a plain div so no interactive elements nest.
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle).toHaveAttribute("type", "button");
    expect(toggle).toHaveClass("min-w-0", "flex-1", "bg-transparent", "text-left");
    const pill = toggle.parentElement!.parentElement as HTMLElement;
    expect(pill).toHaveAttribute("data-testid", "error-pill");
    expect(pill).not.toHaveAttribute("role");
    expect(pill).not.toHaveAttribute("tabindex");
    expect(pill).toHaveClass("rounded-[12px]", "p-[8px]", "w-[560px]", "group/error");
    expect(pill.style.background).toContain("color-mix");
    expect(pill.style.border).toContain("color-mix");
    expect(pill.parentElement).toHaveClass("items-center", "px-[16px]", "mb-[24px]");
    const dashedRule = pill.parentElement!.querySelector('[aria-hidden="true"]');
    expect(dashedRule).toHaveClass("pointer-events-none", "top-[20px]", "h-px");
    expect((dashedRule as HTMLElement).style.background).toContain("repeating-linear-gradient");
    expect(screen.getByTestId("error-headline")).toHaveClass(
      "truncate",
      "whitespace-nowrap",
      "leading-6",
      "text-destructive",
    );
    expect(screen.getByTestId("error-headline")).toHaveAttribute(
      "title",
      "The agent's terminal exited unexpectedly, so the session can't continue.",
    );
    expect(screen.getByRole("button", { name: "Dismiss error message" })).toHaveClass(
      "hover:text-foreground",
    );

    fireEvent.click(toggle);
    const message = screen.getByTestId("error-message-content");
    expect(message.closest("section")!.parentElement).toHaveClass("cursor-auto");
    const copyMessage = screen.getByRole("button", { name: "Copy error message" });
    expect(message).toHaveClass("font-mono", "text-sm", "whitespace-pre-wrap");
    expect(copyMessage).toHaveClass("size-6");
    expect(screen.getByText("Message")).toHaveClass(
      "text-sm",
      "font-medium",
      "leading-4",
      "text-muted-foreground",
    );

    const diagnosticsToggle = screen.getByRole("button", { name: "View diagnostics" });
    expect(diagnosticsToggle).toHaveClass("justify-between", "px-[12px]", "py-[8px]");
    expect(diagnosticsToggle.closest('[data-slot="collapsible"]')).toHaveClass(
      "border-t",
      "-mx-[8px]",
      "-mb-[8px]",
      "w-[calc(100%+16px)]",
    );
    fireEvent.click(diagnosticsToggle);

    expect(screen.getByRole("tablist", { name: "Diagnostic sections" })).toHaveAttribute(
      "data-variant",
      "line",
    );
    const diagnostics = screen.getByTestId("error-diagnostics-content");
    expect(diagnostics).toHaveClass("max-h-64", "text-zinc-100");
    expect(diagnostics.parentElement).toHaveClass("rounded-xl", "bg-zinc-950");
    expect(screen.getByRole("button", { name: "Copy terminal" })).toHaveClass(
      "absolute",
      "top-3",
      "right-3",
    );
  });

  it("preserves classified title, cause, and remediation semantics", () => {
    render(
      <ErrorBanner
        message={TERMINAL_ERROR}
        source="execution"
        code="required_terminal_exited"
        title="Claude Code can't run as root"
        cause="Claude Code refuses this launch mode when the host runs as root."
        remediation="Run the host as a non-root user (uid != 0)."
      />,
    );
    expect(screen.getByText("Claude Code can't run as root")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Claude Code can't run as root/i }));
    expect(screen.getByTestId("error-message-content")).toHaveTextContent(
      "Claude Code refuses this launch mode when the host runs as root.",
    );
    expect(screen.getByTestId("error-message-content")).toHaveTextContent(
      "Try this: Run the host as a non-root user (uid != 0).",
    );
  });

  it("separates terminal diagnostics and last output into tabs", () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    fireEvent.click(screen.getByRole("button", { name: "View diagnostics" }));
    expect(screen.getByRole("tab", { name: "Terminal" })).toHaveAttribute("aria-selected", "true");
    // Clicks inside the expanded body must not collapse the pill.
    fireEvent.click(screen.getByTestId("error-message-content"));
    expect(screen.getByText("Message")).toBeInTheDocument();
    const terminal = screen.getByTestId("error-diagnostics-content");
    expect(terminal).toHaveTextContent("terminal: claude:main");
    expect(terminal).toHaveTextContent("termination_reason: terminal pane no longer available");
    expect(terminal).not.toHaveTextContent("Pane is dead");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Last captured output" }));
    expect(screen.getByRole("tab", { name: "Last captured output" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getAllByTestId("error-diagnostics-content").at(-1)).toHaveTextContent(
      "Pane is dead",
    );
  });

  it("copies message and active diagnostics with accessible confirmation", async () => {
    render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    fireEvent.click(screen.getByRole("button", { name: "Copy error message" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Error message copied" })).toBeTruthy(),
    );
    expect(copyText).toHaveBeenCalledWith(
      "Required terminal exited unexpectedly; the session runtime is no longer available.",
    );
    fireEvent.click(screen.getByRole("button", { name: "View diagnostics" }));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Last captured output" }));
    fireEvent.click(screen.getByRole("button", { name: "Copy last captured output" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Last captured output copied" })).toBeTruthy(),
    );
    expect(copyText).toHaveBeenLastCalledWith("Pane is dead (status 0, Tue Aug 11 17:00:46 2026)");
  });

  it("falls back to a non-empty message without diagnostics controls", () => {
    render(<ErrorBanner message="" source="" code="mystery_failure" />);
    fireEvent.click(screen.getByRole("button", { name: /Something went wrong/ }));
    expect(screen.getByTestId("error-message-content")).toHaveTextContent("mystery_failure");
    expect(screen.queryByRole("button", { name: "View diagnostics" })).toBeNull();
  });

  it("dismisses only the visible banner", () => {
    render(
      <div>
        <span>Unrelated transcript content</span>
        <ErrorBanner message="boom" source="execution" code="executor_error" />
      </div>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Dismiss error message" }));
    expect(screen.queryByTestId("error-headline")).toBeNull();
    expect(screen.getByText("Unrelated transcript content")).toBeInTheDocument();
  });

  it("toggles from the pill body but not from nested action buttons", async () => {
    let resolveRetry: (() => void) | undefined;
    const onRetry = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRetry = resolve;
        }),
    );
    render(
      <ErrorBanner
        message={TERMINAL_ERROR}
        source="execution"
        code="required_terminal_exited"
        onRetry={onRetry}
      />,
    );

    const toggle = screen.getByRole("button", { name: /terminal exited unexpectedly/i });
    const pill = toggle.parentElement!.parentElement as HTMLElement;
    // Clicks on pill padding (outside the headline button) toggle expansion.
    fireEvent.click(pill);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(pill);
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    // Retry starts recovery instead of toggling; dismiss removes the banner.
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("error-reconnecting")).toBeInTheDocument();
    await act(async () => resolveRetry?.());
    expect(screen.queryByTestId("error-headline")).toBeNull();
  });

  it("prevents duplicate retries and removes the replacement pill on success", async () => {
    let resolveRetry: (() => void) | undefined;
    const onRetry = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveRetry = resolve;
        }),
    );
    render(
      <ErrorBanner
        message={TERMINAL_ERROR}
        source="execution"
        code="required_terminal_exited"
        onRetry={onRetry}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    expect(screen.getByText("Message")).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    act(() => {
      retry.click();
      retry.click();
    });
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("error-headline")).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent(/^Reconnecting$/);
    expect(screen.queryByText("Message")).toBeNull();
    resolveRetry?.();
    await waitFor(() => {
      expect(screen.queryByRole("status")).toBeNull();
      expect(screen.queryByTestId("error-headline")).toBeNull();
    });
  });

  it("restores the actionable error and diagnostics when retry fails", async () => {
    const onRetry = vi.fn().mockRejectedValue(new Error("Host is still offline"));
    render(
      <ErrorBanner
        message={TERMINAL_ERROR}
        source="execution"
        code="required_terminal_exited"
        onRetry={onRetry}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Retry failed: Host is still offline"),
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Dismiss error message" })).toHaveFocus();
    // The retry-error status row must not toggle the pill when clicked.
    fireEvent.click(screen.getByRole("status"));
    expect(screen.queryByText("Message")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    expect(screen.getByRole("button", { name: "View diagnostics" })).toBeInTheDocument();
  });

  it("does not invent retry for non-retryable variants", () => {
    render(
      <ErrorBanner
        message="The conversation is too long."
        source="llm"
        code="context_length_exceeded"
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it.each(["executor_error", "connection_error", "runner_error", "wrong_replica"])(
    "does not offer reconnect for live-runner code %s",
    (code) => {
      render(
        <ErrorBanner message="The turn failed." source="execution" code={code} onRetry={vi.fn()} />,
      );
      expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    },
  );

  it("suppresses the runner's unavailable last-output diagnostics tab", () => {
    render(
      <ErrorBanner
        message={[
          "Terminal failed.",
          "",
          "Terminal diagnostics:",
          "pid: 42",
          "",
          "Last captured output: unavailable. The process exited before Omnigent captured a pane snapshot.",
        ].join("\n")}
        source="execution"
        code="required_terminal_exited"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    fireEvent.click(screen.getByRole("button", { name: "View diagnostics" }));
    expect(screen.queryByRole("tab", { name: "Last captured output" })).toBeNull();
    expect(screen.getByTestId("error-diagnostics-content")).toHaveTextContent("pid: 42");
  });

  it("keeps the selected diagnostics tab valid when parsed sections change", () => {
    const { rerender } = render(
      <ErrorBanner message={TERMINAL_ERROR} source="execution" code="required_terminal_exited" />,
    );
    fireEvent.click(screen.getByRole("button", { name: /terminal exited unexpectedly/i }));
    fireEvent.click(screen.getByRole("button", { name: "View diagnostics" }));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Last captured output" }));
    rerender(
      <ErrorBanner
        message={["Terminal failed.", "", "Terminal diagnostics:", "pid: 99"].join("\n")}
        source="execution"
        code="required_terminal_exited"
      />,
    );
    expect(screen.getByTestId("error-diagnostics-content")).toHaveTextContent("pid: 99");
  });
});

describe("RoutingDecisionCard — session-level auto-routing", () => {
  it("applied verdict: shows model pill with tier and rationale", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-opus-4-8"
        applied
        rationale="Multi-file refactor needs deep reasoning."
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    expect(card).toHaveTextContent("Smart routing");
    expect(card).toHaveTextContent("· applied");
    expect(card).toHaveTextContent("Session");
    expect(card).toHaveTextContent("opus");
    expect(card).toHaveTextContent("Multi-file refactor needs deep reasoning.");
    expect(card.getAttribute("data-applied")).toBe("true");
  });

  it("advisory verdict: shows '· advisory' and the model that would have been picked", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-haiku-4-5"
        applied={false}
        rationale="Trivial question."
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    expect(card).toHaveTextContent("· advisory");
    expect(card).toHaveTextContent("haiku");
    expect(card.getAttribute("data-applied")).toBe("false");
  });

  it("shows agent name as row label when mirrored into parent session", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-haiku-4-5"
        applied
        rationale="Simple task."
        agent="claude_code"
      />,
    );
    const card = screen.getByTestId("routing-decision-card");
    // The agent name replaces the generic "Session" label so the
    // orchestrator's transcript identifies which sub-agent was routed.
    expect(card).toHaveTextContent("claude_code");
    expect(card.textContent).not.toContain("Session");
  });

  it("expands raw verdict JSON behind the chevron", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-opus-4-8"
        applied
        rationale="Deep reasoning required."
      />,
    );
    // Collapsed by default — raw JSON not visible.
    expect(screen.queryByText(/"rationale"/)).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.getByText(/"rationale"/)).toBeInTheDocument();
  });
});

describe("routing decision — harness / scope / raw pick", () => {
  // Harness + which sub-agent the decision covers: without the badge a
  // native-subagent decision is indistinguishable from a session one, and a
  // badge on a session/turn decision would invent a sub-agent that has none.
  // Every chip also shortens the harness id: "-native" is how the pane runs,
  // not something a chip needs to say, whatever scope took the decision.
  it.each([
    ["native_subagent", "subagent: researcher"],
    ["turn", null],
    ["session", null],
  ] as const)("card: %s scope renders the sub-agent badge as %s", (scope, badge) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="short task"
        agent="researcher"
        routing={{ harness: "claude-native", scope }}
      />,
    );
    // Anchored: a bare "claude" would also match the unshortened id.
    expect(screen.getByTestId("routing-decision-harness")).toHaveTextContent(/^· claude$/);
    if (badge === null) {
      expect(screen.queryByTestId("routing-decision-scope")).toBeNull();
    } else {
      expect(screen.getByTestId("routing-decision-scope")).toHaveTextContent(badge);
    }
  });

  // The router's vocabulary pick may have had no endpoint and been mapped to a
  // servable id — that must be visible. When it resolves to the same short
  // name there is nothing to disclose, so the row stays off.
  it.each([
    ["gpt-5-6-sol", "gpt-5-6-sol"],
    ["claude-sonnet-5", null],
  ] as const)("card: raw pick %s is disclosed as %s", (rawModel, shown) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="x"
        routing={{ rawModel }}
      />,
    );
    expect(screen.getByTestId("routing-decision-card")).toHaveTextContent("sonnet");
    if (shown === null) {
      expect(screen.queryByTestId("routing-decision-raw-model")).toBeNull();
    } else {
      expect(screen.getByTestId("routing-decision-raw-model")).toHaveTextContent(shown);
    }
  });

  it("card: renders harness, scope badge, raw pick, and the extras in the raw JSON", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="short task"
        agent="researcher"
        routing={{
          harness: "codex-native",
          scope: "child_session",
          decisionId: "dec_123",
          rawModel: "gpt-5-6-sol",
          attemptedOverride: "databricks-claude-opus-4-8",
        }}
      />,
    );
    // child_session is a sub-agent scope, so the harness id renders shortened.
    expect(screen.getByTestId("routing-decision-harness")).toHaveTextContent("codex");
    // The badge's exact wording is pinned once, on subagentScopeLabel.
    expect(screen.getByTestId("routing-decision-scope")).toBeTruthy();
    expect(screen.getByTestId("routing-decision-raw-model")).toHaveTextContent("gpt-5-6-sol");
    // The overridden ask is the point of the row, so it shows at a glance; the
    // decision id stays audit data behind the chevron.
    expect(screen.getByTestId("routing-decision-attempted-override")).toHaveTextContent("opus");
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.getByText(/"decision_id"/)).toBeInTheDocument();
    expect(screen.getByText(/"attempted_override"/)).toBeInTheDocument();
  });

  // Strict adherence to the router means a spawn's own model ask is applied
  // only when the router agrees; when it doesn't, the substitution has to be
  // visible without expanding the verdict.
  it.each([
    ["databricks-claude-opus-4-8", "opus"],
    // Same short name as the pill — a struck-through duplicate reads as a bug.
    ["system.ai.claude-sonnet-5", null],
  ] as const)("card: attempted override %s is disclosed as %s", (attemptedOverride, shown) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="Spawn requested databricks-claude-opus-4-8; overridden."
        routing={{ attemptedOverride }}
      />,
    );
    if (shown === null) {
      expect(screen.queryByTestId("routing-decision-attempted-override")).toBeNull();
    } else {
      const span = screen.getByTestId("routing-decision-attempted-override");
      expect(span).toHaveTextContent(shown);
      expect(span.className).toContain("line-through");
      // Only the first of (attempted, raw pick, pill) pushes the group right.
      expect(span.className).toContain("ml-auto");
    }
  });

  it("card: the attempted override takes ml-auto ahead of the raw pick", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="x"
        routing={{ attemptedOverride: "databricks-claude-opus-4-8", rawModel: "gpt-5-6-sol" }}
      />,
    );
    expect(screen.getByTestId("routing-decision-attempted-override").className).toContain(
      "ml-auto",
    );
    // Exactly one spacer, else the row splits into two right-aligned groups.
    expect(screen.getByTestId("routing-decision-raw-model").className).not.toContain("ml-auto");
  });

  it("card: omits the new rows and JSON keys when the fields are absent", () => {
    render(
      <RoutingDecisionCard model="databricks-claude-opus-4-8" applied rationale="deep reasoning" />,
    );
    expect(screen.queryByTestId("routing-decision-harness")).toBeNull();
    expect(screen.queryByTestId("routing-decision-scope")).toBeNull();
    expect(screen.queryByTestId("routing-decision-raw-model")).toBeNull();
    expect(screen.queryByTestId("routing-decision-attempted-override")).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.queryByText(/"harness"/)).toBeNull();
    expect(screen.queryByText(/"raw_model"/)).toBeNull();
  });

  // Only an AI-Gateway-routed decision is marked: the built-in judge is the
  // plain case, and a legacy row predates the field entirely.
  const AIGW_MARK = "routing-decision-source-databricks";
  const AIGW_NAME = "Routed by the Databricks AI Gateway";

  it("card: marks a decision the Databricks AI Gateway answered", () => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="x"
        routing={{ routerSource: "databricks-aigw" }}
      />,
    );
    expect(screen.getByTestId(AIGW_MARK)).toBeTruthy();
    // The wrapper carries the name — the brand mark itself is aria-hidden, and
    // it must never announce as the model family ("DBRX").
    expect(screen.getByRole("img", { name: AIGW_NAME })).toBeTruthy();
    expect(screen.queryByTitle(/DBRX/i)).toBeNull();
    expect(screen.queryByLabelText(/DBRX/i)).toBeNull();
    expect(screen.queryByText(/DBRX/i)).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.getByText(/"router_source"/)).toBeInTheDocument();
  });

  it.each([
    ["the built-in judge answered", { routerSource: "oss-llm" }],
    ["the field is absent", {}],
  ] as const)("card: leaves the mark off when %s", (_case, routing) => {
    render(
      <RoutingDecisionCard
        model="databricks-claude-sonnet-5"
        applied
        rationale="x"
        routing={routing}
      />,
    );
    expect(screen.queryByTestId(AIGW_MARK)).toBeNull();
    expect(screen.queryByRole("img", { name: AIGW_NAME })).toBeNull();
  });

  it("card: leaves the mark off a legacy row with no routing at all", () => {
    render(<RoutingDecisionCard model="databricks-claude-sonnet-5" applied rationale="x" />);
    expect(screen.queryByTestId(AIGW_MARK)).toBeNull();
    fireEvent.click(screen.getByTestId("routing-decision-raw-toggle"));
    expect(screen.queryByText(/"router_source"/)).toBeNull();
  });
});
