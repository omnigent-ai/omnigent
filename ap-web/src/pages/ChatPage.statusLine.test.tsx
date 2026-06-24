import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";
import { Composer, formatModelEffortStatusLabel } from "./ChatPage";

// Pins the visibility rules for the status-line tray under the composer:
// it shows the worktree branch (truncated so the tray never wraps), current
// model/effort, and the context ring. It must not render at all when none
// has data — no dead shelf attached to the composer. Session cost was moved
// OUT of this tray into the header agent-info popover, so a priced cost must
// NOT resurrect the tray or appear here.

/** Minimal ComposerProps for an interactive (writable, idle) composer. */
function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    agentsLoading: false,
    selectedAgentId: null,
    onSelectAgent: vi.fn(),
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function renderComposer() {
  return render(
    <TooltipProvider>
      <Composer {...composerProps()} />
    </TooltipProvider>,
  );
}

/** The status-line tray — absent when no branch / ring has data. */
function statusLine(): Element | null {
  return document.querySelector('[data-testid="composer-status-line"]');
}

describe("Composer status line (branch + context ring)", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      contextWindow: null,
      tokensUsed: null,
      sessionCostUsd: null,
      gitBranch: null,
      llmModel: null,
      selectedModel: null,
      selectedEffort: null,
      codexModelOptions: [],
      codexPlanMode: false,
      nativeVendorOwnsModel: false,
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("never renders the session cost in the status line", () => {
    // Cost moved to the agent-info popover. A priced cost here would mean
    // the move regressed and the cost is being shown in two places.
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 25_000, sessionCostUsd: 1.23 });
    renderComposer();
    expect(screen.queryByText(/session cost/i)).toBeNull();
    expect(screen.queryByText("$1.23")).toBeNull();
  });

  it("omits the tray when neither branch nor ring is visible", () => {
    // No branch, no context info — and a priced cost must not resurrect
    // the tray now that cost lives elsewhere.
    useChatStore.setState({ sessionCostUsd: 0.5 });
    renderComposer();
    expect(statusLine()).toBeNull();
  });

  it("shows the context ring with the correct used percentage", () => {
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 25_000 });
    renderComposer();
    expect(statusLine()).not.toBeNull();
    // 25k of 100k → 25% used; a wrong value means the ring wired the
    // wrong store fields through its props.
    expect(screen.getByLabelText("25% of context used")).toBeInTheDocument();
  });

  it("shows model and effort immediately left of the context ring", () => {
    useChatStore.setState({
      selectedModel: "gpt-5.5",
      selectedEffort: "xhigh",
      contextWindow: 100_000,
      tokensUsed: 25_000,
      codexModelOptions: [
        {
          id: "gpt-5.5",
          model: "databricks-gpt-5-5",
          displayName: "Codex GPT 5.5 Preview",
          defaultReasoningEffort: "high",
          supportedReasoningEfforts: [
            { reasoningEffort: "low", description: "Low" },
            { reasoningEffort: "medium", description: "Medium" },
            { reasoningEffort: "high", description: "High" },
            { reasoningEffort: "xhigh", description: "Extra high" },
          ],
          isDefault: true,
        },
      ],
    });
    renderComposer();

    const modelEffort = screen.getByTestId("composer-model-effort");
    const ring = screen.getByLabelText("25% of context used");
    expect(modelEffort).toHaveTextContent("Codex GPT 5.5 Preview xhigh");
    expect(modelEffort.compareDocumentPosition(ring) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("falls back to the bound model when there is no model override", () => {
    useChatStore.setState({
      llmModel: "databricks-gpt-5-5",
      selectedEffort: "medium",
    });
    renderComposer();

    expect(screen.getByTestId("composer-model-effort")).toHaveTextContent(
      "databricks-gpt-5-5 Medium",
    );
    expect(screen.queryByLabelText(/context used/)).toBeNull();
  });

  it("hides the model/effort label for vendor-owned native sessions (e.g. Qwen)", () => {
    // qwen/goose/cursor/pi/opencode pick their model inside the vendor TUI, so
    // Omnigent's bound llmModel is just an unused default — showing it (e.g.
    // "claude-sonnet-4-6") on a Qwen session is misleading, so it's hidden.
    useChatStore.setState({
      llmModel: "claude-sonnet-4-6",
      selectedEffort: "medium",
      nativeVendorOwnsModel: true,
    });
    renderComposer();

    expect(screen.queryByTestId("composer-model-effort")).toBeNull();
  });

  it("draws the ring arc as what's used, not what's left", () => {
    // 25k of 100k → the visible arc must encode the 25% USED, so the
    // ring starts empty and fills as context is consumed. If the arc
    // encoded the 75% remaining instead, a fresh session would show a
    // full ring — the confusing state this guards against.
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 25_000 });
    renderComposer();
    const ring = screen.getByLabelText("25% of context used");
    // The track is the first circle; the second is the used arc.
    const arc = ring.querySelectorAll("circle")[1];
    const circumference = 2 * Math.PI * 5.5;
    const dash = arc.getAttribute("stroke-dasharray") ?? "";
    const drawn = Number.parseFloat(dash.split(" ")[0]);
    expect(drawn).toBeCloseTo(0.25 * circumference, 3);
    // Belt and suspenders: it must NOT be the 75%-remaining arc.
    expect(drawn).not.toBeCloseTo(0.75 * circumference, 3);
  });

  it("renders no arc circle at 0% used", () => {
    // A zero-length dash with round linecaps still paints the caps — a
    // phantom dot at 12 o'clock suggesting usage on a fresh session.
    // Only the track circle may render.
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 0 });
    renderComposer();
    const ring = screen.getByLabelText("0% of context used");
    expect(ring.querySelectorAll("circle")).toHaveLength(1);
  });

  it("hides the ring on a zero context window instead of rendering NaN", () => {
    // The SSE usage path rejects context_window <= 0 but the session
    // snapshot path passes it through; 0/0 would render "NaN%".
    useChatStore.setState({ contextWindow: 0, tokensUsed: 0 });
    renderComposer();
    expect(statusLine()).toBeNull();
    expect(screen.queryByLabelText(/context used/)).toBeNull();
  });

  it("shows the worktree branch on the left and truncates it", () => {
    useChatStore.setState({
      gitBranch: "feature/a-very-long-worktree-branch-name-that-would-wrap",
    });
    renderComposer();
    const branch = screen.getByTestId("composer-git-branch");
    expect(branch).toHaveTextContent("feature/a-very-long-worktree-branch-name-that-would-wrap");
    // `truncate` (overflow-hidden + ellipsis + nowrap) is the guard that
    // keeps a long branch from wrapping the tray onto a second line.
    expect(branch).toHaveClass("truncate");
  });

  it("renders the tray with a branch even when the ring is absent", () => {
    // The branch alone is enough to surface the tray — the visibility
    // guard must not key off the ring only.
    useChatStore.setState({ gitBranch: "main" });
    renderComposer();
    expect(statusLine()).not.toBeNull();
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("main");
  });

  it("shows no branch when the session uses no worktree", () => {
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 25_000, gitBranch: null });
    renderComposer();
    expect(statusLine()).not.toBeNull();
    expect(screen.queryByTestId("composer-git-branch")).toBeNull();
  });

  it("shows a persistent Plan mode badge when Codex Plan mode is active", () => {
    useChatStore.setState({ codexPlanMode: true });
    renderComposer();

    expect(statusLine()).not.toBeNull();
    expect(screen.getByTestId("composer-plan-mode")).toHaveTextContent("Plan mode");
  });

  it("places Plan mode to the left of model and effort", () => {
    useChatStore.setState({
      codexPlanMode: true,
      selectedModel: "gpt-5.5",
      selectedEffort: "xhigh",
    });
    renderComposer();

    const plan = screen.getByTestId("composer-plan-mode");
    const modelEffort = screen.getByTestId("composer-model-effort");
    expect(plan.compareDocumentPosition(modelEffort) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });
});

describe("formatModelEffortStatusLabel", () => {
  it("uses Codex display names exactly as returned in model metadata", () => {
    expect(
      formatModelEffortStatusLabel("gpt-5.5", "xhigh", [
        {
          id: "gpt-5.5",
          model: "databricks-gpt-5-5",
          displayName: "codex says GPT-5.5",
          defaultReasoningEffort: "high",
          supportedReasoningEfforts: [
            { reasoningEffort: "low", description: "Low" },
            { reasoningEffort: "medium", description: "Medium" },
            { reasoningEffort: "high", description: "High" },
            { reasoningEffort: "xhigh", description: "Extra high" },
          ],
          isDefault: true,
        },
      ]),
    ).toBe("codex says GPT-5.5 xhigh");
  });

  it("leaves unknown model ids raw", () => {
    expect(formatModelEffortStatusLabel("gpt-5.5", "xhigh")).toBe("gpt-5.5 xHigh");
    expect(formatModelEffortStatusLabel("databricks-gpt-5-5", "xhigh")).toBe(
      "databricks-gpt-5-5 xHigh",
    );
  });

  it("omits missing pieces", () => {
    expect(formatModelEffortStatusLabel("opus", null)).toBe("Opus");
    expect(formatModelEffortStatusLabel(null, "low")).toBe("Low");
    expect(formatModelEffortStatusLabel(null, null)).toBeNull();
  });
});
