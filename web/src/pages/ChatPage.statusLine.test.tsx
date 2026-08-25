import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { useChatStore } from "@/store/chatStore";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions); these status-line tests don't exercise it, so stub the hook to
// avoid wrapping every render in a QueryClientProvider.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});

// HostBadge now lives in the status-line tray (left of the worktree branch).
// It reads the session's host binding via these hooks; stub them so the badge
// renders deterministically without a QueryClient / RunnerHealth provider. The
// default is "not host-bound", so the badge self-hides and the existing branch/
// ring/harness assertions are unchanged; host-aware tests override per case.
const { useSessionMock, useHostsMock, useSessionHostOnlineMock } = vi.hoisted(() => ({
  useSessionMock: vi.fn(),
  useHostsMock: vi.fn(),
  useSessionHostOnlineMock: vi.fn(),
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: (id: string | null | undefined) => useSessionMock(id),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: (opts: unknown) => useHostsMock(opts),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: (id: string | undefined) => useSessionHostOnlineMock(id),
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof AgentLabelsModule>()),
  useBrainHarnessLabels: () => ({
    "claude-sdk": "Claude SDK",
    codex: "Codex",
    cursor: "Cursor",
    pi: "Pi",
    antigravity: "Antigravity",
    copilot: "Copilot",
  }),
}));

import { BRAIN_HARNESS_LABELS } from "@/lib/agentLabels";
import { Composer, composerHarnessLabel, formatModelEffortStatusLabel } from "./ChatPage";

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
    selectedAgentId: null,
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

function renderComposer(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return render(
    <TooltipProvider>
      <Composer {...composerProps(overrides)} />
    </TooltipProvider>,
  );
}

/** The status-line tray — absent when no branch / ring has data. */
function statusLine(): Element | null {
  return document.querySelector('[data-testid="composer-status-line"]');
}

/** Bind the active session to an online host named `name` so HostBadge shows. */
function bindHost(name: string) {
  useSessionMock.mockReturnValue({
    session: { hostId: "host_a1b2" },
    isLoading: false,
    error: null,
  });
  useHostsMock.mockReturnValue({
    data: [
      { host_id: "host_a1b2", name, owner: "alice", status: "online", sandbox_provider: null },
    ],
  });
  useSessionHostOnlineMock.mockReturnValue(true);
}

describe("Composer status line (branch + context ring)", () => {
  beforeEach(() => {
    // Default: no host bound, so HostBadge renders nothing.
    useSessionMock.mockReset().mockReturnValue({
      session: { hostId: null },
      isLoading: false,
      error: null,
    });
    useHostsMock.mockReset().mockReturnValue({ data: [] });
    useSessionHostOnlineMock.mockReset().mockReturnValue(undefined);
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
      sessionHarness: null,
      subAgentName: null,
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

  it("no longer renders the harness label in the status tray (moved to the config gear)", () => {
    // The harness identity moved from the tray into the config gear's hover
    // tooltip, so it must never resurface in the status line — for a native
    // vendor session or an SDK/bundle session.
    useChatStore.setState({ contextWindow: 100_000, tokensUsed: 25_000, sessionHarness: "pi" });
    renderComposer({
      modelPickerKind: "codex",
      agents: [{ id: "a1", name: "polly" }],
      selectedAgentId: "a1",
    });

    expect(screen.queryByTestId("composer-harness")).toBeNull();
  });

  it("no longer renders model/effort in the status tray (moved to the composer label)", () => {
    // The swap moved the model/effort label out of the tray and into the
    // composer's read-only label, so it must never resurface here — even for a
    // vendor-owned native session where the model used to be (wrongly) shown.
    useChatStore.setState({
      llmModel: "claude-sonnet-4-6",
      selectedEffort: "medium",
      nativeVendorOwnsModel: true,
      contextWindow: 100_000,
      tokensUsed: 25_000,
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

  it("places Plan mode to the left of the context ring", () => {
    useChatStore.setState({ codexPlanMode: true, contextWindow: 100_000, tokensUsed: 25_000 });
    renderComposer({ modelPickerKind: "codex" });

    const plan = screen.getByTestId("composer-plan-mode");
    const ring = screen.getByLabelText("25% of context used");
    expect(plan.compareDocumentPosition(ring) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("renders the tray for a host-bound session with no branch or ring", () => {
    // Regression: removing the harness label from the tray must not take the
    // host badge + context footer with it. A host-bound session (e.g. a codex
    // session with no worktree branch, before the ring populates) still shows
    // the tray so the host indicator is visible.
    bindHost("mac-laptop");
    useChatStore.setState({ gitBranch: null, contextWindow: null, tokensUsed: null });
    renderComposer();

    expect(statusLine()).not.toBeNull();
    expect(screen.getByTestId("host-badge")).toHaveTextContent("mac-laptop");
  });

  it("shows the host badge to the left of the worktree branch", () => {
    // The host indicator moved out of the chat header into this tray; it
    // sits immediately left of the worktree branch.
    bindHost("mac-laptop");
    useChatStore.setState({ gitBranch: "geist" });
    renderComposer();

    const host = screen.getByTestId("host-badge");
    const branch = screen.getByTestId("composer-git-branch");
    expect(host).toHaveTextContent("mac-laptop");
    expect(host.compareDocumentPosition(branch) & Node.DOCUMENT_POSITION_FOLLOWING).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
  });

  it("turns the host badge into a clickable reconnect prompt for an offline host", () => {
    // An offline host surfaces the reconnect affordance in the host badge (in
    // place of the old banner below the composer) while keeping the host name:
    // the tray shows even with no branch/ring, and clicking opens the help.
    bindHost("mac-laptop");
    useSessionHostOnlineMock.mockReturnValue(false);
    useChatStore.setState({ gitBranch: null, contextWindow: null, tokensUsed: null });
    const onShowReconnectHelp = vi.fn();
    renderComposer({ onShowReconnectHelp });

    expect(statusLine()).not.toBeNull();
    const badge = screen.getByTestId("host-badge");
    expect(badge.tagName).toBe("BUTTON");
    expect(badge).toHaveTextContent("mac-laptop");
    badge.click();
    expect(onShowReconnectHelp).toHaveBeenCalledTimes(1);
  });

  it("keeps the tray hidden for a session with no host and nothing else to show", () => {
    // onShowReconnectHelp is now passed for every session, so it must not
    // resurrect an empty tray on a session with no host badge to hang it on.
    useChatStore.setState({ gitBranch: null, contextWindow: null, tokensUsed: null });
    renderComposer({ onShowReconnectHelp: vi.fn() });

    expect(statusLine()).toBeNull();
  });

  it("hides the host badge on a sub-agent session", () => {
    // A child session repurposes the header's left slot for the back
    // affordance, so the host badge stays hidden there as it did before. Sub-
    // agents are never host-bound (host_id is null), so they can't be
    // host_offline anyway — a stranded child is local_stranded, handled by the
    // banner elsewhere.
    bindHost("mac-laptop");
    useChatStore.setState({ gitBranch: "geist" });
    renderComposer({ subAgentLabel: "check-eligibility" });

    expect(screen.queryByTestId("host-badge")).toBeNull();
    expect(screen.getByTestId("composer-git-branch")).toBeInTheDocument();
  });
});

describe("composerHarnessLabel", () => {
  it("reads native wrappers as the bare vendor name", () => {
    expect(composerHarnessLabel("claude", null, "claude-native")).toBe("Claude");
    expect(composerHarnessLabel("codex", null, "codex-native")).toBe("Codex");
  });

  it("reads SDK agents as '<Agent> (<Harness>)'", () => {
    expect(composerHarnessLabel(null, "polly", "pi")).toBe("Polly (Pi)");
  });

  // A Claude Code sub-agent's sub_agent_name is the Task tool's
  // `subagent_type` ("general-purpose"), and the child reuses the parent's
  // claude-native agent row — so without the wrapper the label reads
  // "General-purpose" instead of naming the product running it.
  it("reads a native sub-agent child as its vendor, not the vendor-side agent type", () => {
    expect(
      composerHarnessLabel(
        null,
        "general-purpose",
        "claude-native",
        BRAIN_HARNESS_LABELS,
        "claude-code-native-ui-subagent",
      ),
    ).toBe("Claude Code");
    expect(
      composerHarnessLabel(
        null,
        "reviewer",
        null,
        BRAIN_HARNESS_LABELS,
        "codex-native-ui-subagent",
      ),
    ).toBe("Codex");
  });

  it("falls back to the agent name alone when the harness is unmapped", () => {
    expect(composerHarnessLabel(null, "polly", "some-unknown-harness")).toBe("Polly");
    expect(composerHarnessLabel(null, "polly", null)).toBe("Polly");
  });

  it("returns null when nothing is known", () => {
    expect(composerHarnessLabel(null, null, null)).toBeNull();
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

  it("prefers the catalog display name for a Claude [1m] alias", () => {
    expect(
      formatModelEffortStatusLabel("sonnet[1m]", "high", [
        { id: "sonnet[1m]", model: "claude-sonnet-5[1m]", displayName: "Sonnet 5 (1M context)" },
      ]),
    ).toBe("Sonnet 5 (1M context) high");
  });

  it("renders a catalog-less Claude [1m] alias friendly without claiming a version", () => {
    expect(formatModelEffortStatusLabel("sonnet[1m]", "high")).toBe("Sonnet (1M context) High");
    expect(formatModelEffortStatusLabel("opus[1m]", null)).toBe("Opus (1M context)");
  });

  it("title-cases catalog-less alias-shaped ids mechanically", () => {
    expect(formatModelEffortStatusLabel("sonnet", null)).toBe("Sonnet");
    expect(formatModelEffortStatusLabel("sonnet_5", null)).toBe("Sonnet 5");
    expect(formatModelEffortStatusLabel("fable", null)).toBe("Fable");
  });

  it("omits missing pieces", () => {
    expect(formatModelEffortStatusLabel("opus", null)).toBe("Opus");
    expect(formatModelEffortStatusLabel(null, "low")).toBe("Low");
    expect(formatModelEffortStatusLabel(null, null)).toBeNull();
  });
});
