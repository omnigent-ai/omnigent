import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as GoalApiModule from "@/lib/goalApi";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createRef, StrictMode, type ComponentRef, type ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import {
  clearSessionDrafts,
  getSessionDraft,
  hasSessionDraft,
  setSessionDraft,
} from "@/lib/sessionDrafts";
import { setOmnigentHostConfig } from "@/lib/host";
import { serializeReplyDraft, type StoredReplyDraft } from "@/lib/replyDraft";
import { COMPOSER_SEND_SHORTCUT_STORAGE_KEY } from "@/lib/composerSendShortcutPreferences";
import { CHAT_COLUMN_WIDTH } from "./chatLayout";

// Composer reads workspace files via a TanStack query hook (for "@"-file
// mentions). These slash-command tests don't exercise that, so stub the hook
// to avoid needing a QueryClientProvider around every bare render.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});

// ComposerStatusLine's PR link reads GitHub info via a TanStack query; stub it
// (default: no PR) so bare Composer renders don't need a QueryClientProvider.
vi.mock("@/hooks/useGithub", () => ({
  useGithubInfo: () => ({ data: undefined }),
}));
// The workspace bar's git-status hook uses TanStack Query; stub it so the
// composer renders in isolation (no QueryClient) with a neutral empty status.
// The hoisted spy records the args so a test can assert the page passes the
// real session id / host / workspace / creation branch (not fixtures).
const { composerGitStatusArgsSpy } = vi.hoisted(() => ({ composerGitStatusArgsSpy: vi.fn() }));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: (args: unknown) => {
    composerGitStatusArgsSpy(args);
    return {
      branch: null,
      branchState: "unknown",
      isWorktree: null,
      worktreePath: null,
      creationBranch: null,
      repoNameWithOwner: null,
      prCount: 0,
      prNumber: null,
      refresh: () => {},
      refreshing: false,
    };
  },
}));
// SubagentTaskIndicator's child-session query also needs a QueryClient; stub it
// so the indicator self-hides (no active children) in isolated composer renders.
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: () => ({ children: [] }),
}));
// HostBadge now renders in the composer's status-line tray and reads the
// session's host binding via TanStack Query. Stub the hooks so it self-hides
// (no host bound) without needing a QueryClient provider around these renders.
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHostsModule>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof RunnerHealthProviderModule>()),
  useSessionHostOnline: () => undefined,
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
vi.mock("@/lib/goalApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GoalApiModule>()),
  getGoal: vi.fn(),
}));
import type { ElicitationBlock } from "@/lib/blocks";
import { getGoal } from "@/lib/goalApi";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Composer, shouldQueueSend } from "./ChatPage";
import type { QueuedMessage } from "@/store/chatStore";
import {
  BUILTIN_SLASH_COMMANDS,
  rankedSlashCommandNames,
  SlashCommandMenu,
  slashCommandMatches,
} from "@/components/SlashCommandMenu";

// These tests pin the slash-command suggestions menu UX in the composer:
// (1) the first match is highlighted as soon as the menu opens, so Tab/Enter
// complete it without arrowing down first, and (2) the highlighted row is
// scrolled into view as the user navigates. Both regressed because the menu
// previously opened with nothing pre-selected (menuIndex === -1), so Tab fell
// through to the browser's default focus move and Enter sent the message.

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
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

async function openSessionModels() {
  fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
  fireEvent.click(screen.getByTestId("composer-agent-edit"));
  await screen.findByTestId("composer-agent-config-menu");
}

function openSessionConfig() {
  fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
}

const CLAUDE_MODEL_OPTIONS = [
  { id: "fable", displayName: "Fable" },
  { id: "opus", displayName: "Opus" },
  { id: "sonnet", displayName: "Sonnet 4.6" },
  { id: "sonnet_5", displayName: "Sonnet 5" },
  { id: "haiku", displayName: "Haiku" },
];

/** The composer textarea, located by its aria-label. */
function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

function forceDesktopCoarsePointer(): () => void {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes("pointer: coarse"),
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  return () => {
    window.matchMedia = original;
  };
}

/** The currently highlighted menu row, or null when none is highlighted. */
function activeRow(): HTMLElement | null {
  return document.querySelector('[data-active="true"]');
}

function renderWithTooltips(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

function tooltipKeys(tooltip: HTMLElement): string[] {
  return Array.from(tooltip.querySelectorAll('[data-slot="kbd"]')).map(
    (key) => key.textContent ?? "",
  );
}

describe("Composer Escape interrupt", () => {
  beforeEach(() => {
    clearSessionDrafts();
    useChatStore.setState({ conversationId: "conv_escape", blocks: [] });
  });

  afterEach(() => {
    cleanup();
    clearSessionDrafts();
  });

  it.each(["idle", "streaming"] as const)(
    "interrupts a working session with local status %s without losing the draft",
    (status) => {
      const props = composerProps({ status, isWorking: true });
      render(<Composer {...props} />);

      expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
      fireEvent.keyDown(textarea(), { key: "Escape" });
      expect(props.onStop).toHaveBeenCalledTimes(1);

      fireEvent.change(textarea(), { target: { value: "unfinished follow-up" } });
      fireEvent.keyDown(textarea(), { key: "Escape" });
      expect(props.onStop).toHaveBeenCalledTimes(2);
      expect(textarea()).toHaveValue("unfinished follow-up");
      expect(props.onSend).not.toHaveBeenCalled();
    },
  );

  it.each(["idle", "streaming"] as const)(
    "does not interrupt an inactive session with local status %s",
    (status) => {
      const props = composerProps({ status });
      render(<Composer {...props} />);
      fireEvent.change(textarea(), { target: { value: "unfinished message" } });
      fireEvent.keyDown(textarea(), { key: "Escape" });
      expect(props.onStop).not.toHaveBeenCalled();
      expect(textarea()).toHaveValue("unfinished message");
    },
  );

  it.each([{ permissionLevel: 1 }, { readOnlyReason: "Session is read-only" }])(
    "does not interrupt a read-only session: %j",
    (overrides) => {
      const props = composerProps({ status: "streaming", isWorking: true, ...overrides });
      render(<Composer {...props} />);
      expect(screen.getByRole("button", { name: "Interrupt" })).toBeDisabled();
      fireEvent.keyDown(textarea(), { key: "Escape" });
      expect(props.onStop).not.toHaveBeenCalled();
    },
  );

  it("dismisses slash suggestions before interrupting", () => {
    const props = composerProps({ isWorking: true });
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(activeRow()).not.toBeNull();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(props.onStop).not.toHaveBeenCalled();
    expect(activeRow()).toBeNull();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(props.onStop).toHaveBeenCalledTimes(1);
  });

  it("leaves Escape to active IME composition", () => {
    const props = composerProps({ isWorking: true });
    render(<Composer {...props} />);
    fireEvent.compositionStart(textarea());
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(props.onStop).not.toHaveBeenCalled();
    fireEvent.compositionEnd(textarea());
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(props.onStop).toHaveBeenCalledTimes(1);
  });
});

describe("Composer session drafts", () => {
  beforeEach(() => {
    clearSessionDrafts();
    useChatStore.setState({ conversationId: "conv_draft" });
  });

  afterEach(() => {
    cleanup();
    clearSessionDrafts();
  });

  it("publishes unfinished text for the sidebar and clears it after send", async () => {
    render(<Composer {...composerProps()} />);

    fireEvent.change(textarea(), { target: { value: "unfinished message" } });
    await waitFor(() => expect(hasSessionDraft("conv_draft")).toBe(true));

    fireEvent.submit(textarea().closest("form")!);
    await waitFor(() => expect(hasSessionDraft("conv_draft")).toBe(false));
  });
});

describe("Composer growth layout", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shares the responsive chat width with its workspace controls", () => {
    render(<Composer {...composerProps()} />);

    const card = textarea().closest("[data-composer-card]");
    const workspace = screen.getByTestId("composer-workspace-controls").parentElement;
    for (const element of [card, workspace]) {
      expect(element).toHaveClass("w-full", ...CHAT_COLUMN_WIDTH.split(" "));
      expect(element).not.toHaveClass("max-w-[720px]");
    }
  });

  it("keeps multiline growth in layout instead of offsetting the form over the transcript", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    const form = ta.closest("form");
    expect(form).not.toBeNull();

    const originalGetComputedStyle = window.getComputedStyle.bind(window);
    vi.spyOn(window, "getComputedStyle").mockImplementation((element, pseudoElt) => {
      if (element === ta) {
        return {
          lineHeight: "20px",
          paddingTop: "0px",
          paddingBottom: "0px",
          minHeight: "0px",
        } as CSSStyleDeclaration;
      }
      return originalGetComputedStyle(element, pseudoElt);
    });
    Object.defineProperty(ta, "scrollHeight", {
      configurable: true,
      get: () => 220,
    });

    fireEvent.change(ta, { target: { value: "one\ntwo\nthree\nfour" } });

    expect(ta.style.height).toBe("200px");
    expect(form?.style.marginTop).toBe("");
  });

  it("keeps long drafts scrollable without showing a native scrollbar", () => {
    render(<Composer {...composerProps()} />);

    const ta = textarea();
    expect(ta).toHaveClass(
      "overflow-y-auto",
      "[scrollbar-width:none]",
      "[&::-webkit-scrollbar]:hidden",
    );
    expect(ta.parentElement).toHaveClass("overflow-hidden");
  });
});

describe("Composer send shortcut", () => {
  beforeEach(() => {
    localStorage.clear();
    clearSessionDrafts();
    useChatStore.setState({
      conversationId: "conv_shortcut",
      skills: [{ name: "deslop", description: "Remove AI slop" }],
    });
  });

  afterEach(() => {
    cleanup();
    localStorage.clear();
    clearSessionDrafts();
  });

  it("keeps Enter and the legacy Mod+Enter alias in default mode", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "legacy alias" } });
    fireEvent.keyDown(textarea(), { key: "Enter", metaKey: true });
    expect(onSend).toHaveBeenCalledWith("legacy alias", undefined);

    fireEvent.change(textarea(), { target: { value: "default shortcut" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).toHaveBeenLastCalledWith("default shortcut", undefined);
  });

  it("uses Mod+Enter after the alternate preference is restored", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "alternate shortcut" } });

    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(textarea(), { key: "Enter", metaKey: true });
    expect(onSend.mock.calls[0]?.[0]).toBe("alternate shortcut");
  });

  it("shows the alternate Send shortcut in the button tooltip", async () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "ready to send" } });

    fireEvent.pointerMove(screen.getByRole("button", { name: "Send" }), {
      pointerType: "mouse",
    });
    const tooltip = await screen.findByRole("tooltip");

    expect(within(tooltip).getByText("Send")).toBeInTheDocument();
    expect(tooltipKeys(tooltip)).toEqual(["Ctrl", "↵"]);
  });

  it("keeps Enter native and hides its hint on a desktop-width coarse pointer", () => {
    const restorePointer = forceDesktopCoarsePointer();
    const onSend = vi.fn();
    try {
      render(<Composer {...composerProps({ onSend })} />);
      fireEvent.change(textarea(), { target: { value: "/des" } });
      fireEvent.keyDown(textarea(), { key: "Enter" });
      expect(textarea().value).toBe("/des");
      expect(onSend).not.toHaveBeenCalled();

      fireEvent.focus(screen.getByRole("button", { name: "Send" }));
      expect(screen.queryByRole("tooltip")).toBeNull();
    } finally {
      restorePointer();
    }
  });

  it("keeps plain Enter completion while Mod+Enter bypasses an open slash menu", () => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, "true");
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    fireEvent.change(textarea(), { target: { value: "/des" } });

    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(textarea().value).toBe("/deslop ");
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(textarea(), { target: { value: "/des" } });
    fireEvent.keyDown(textarea(), { key: "Enter", ctrlKey: true });
    expect(onSend.mock.calls[0]?.[0]).toBe("/des");
  });
});

describe("Composer Claude goal control", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("sends the completion condition as a Claude /goal command", async () => {
    const onSend = vi.fn();
    useChatStore.setState({ conversationId: "conv_polly" });
    renderWithTooltips(<Composer {...composerProps({ onSend, showClaudeGoalControl: true })} />);

    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-goal-action"));
    await screen.findByTestId("goal-condition");
    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  Finish the implementation and pass tests  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onSend).toHaveBeenCalledWith("/goal Finish the implementation and pass tests");
  });
});

describe("Composer Codex goal control", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("sends the completion condition as a Codex /goal command", async () => {
    const onSend = vi.fn();
    useChatStore.setState({ conversationId: "conv_polly" });
    renderWithTooltips(
      <Composer {...composerProps({ onSend, showPollyCodexGoalControl: true })} />,
    );

    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-goal-action"));
    await screen.findByTestId("goal-condition");
    expect(screen.getByText(/Codex keeps working until this condition is met/)).toBeInTheDocument();
    fireEvent.change(screen.getByTestId("goal-condition"), {
      target: { value: "  Finish the implementation and pass tests  " },
    });
    fireEvent.click(screen.getByTestId("goal-start"));

    expect(onSend).toHaveBeenCalledWith("/goal Finish the implementation and pass tests");
  });
});

describe("Composer native goal state", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads the goal when a detached runner reconnects", async () => {
    const mockGetGoal = vi.mocked(getGoal);
    mockGetGoal.mockResolvedValueOnce({
      goal: {
        objective: "Finish the implementation",
        status: "active",
        tokenBudget: null,
        tokensUsed: 0,
        timeUsedSeconds: 0,
        createdAt: null,
        updatedAt: null,
      },
    });
    useChatStore.setState({ conversationId: "conv_goal" });

    const { rerender } = renderWithTooltips(
      <Composer {...composerProps({ showGoalControl: true, runnerOnline: false })} />,
    );
    expect(mockGetGoal).not.toHaveBeenCalled();

    rerender(
      <TooltipProvider>
        <Composer {...composerProps({ showGoalControl: true, runnerOnline: true })} />
      </TooltipProvider>,
    );

    await waitFor(() => expect(mockGetGoal).toHaveBeenCalledWith("conv_goal"));
  });
});

describe("Composer slash-command menu", () => {
  beforeEach(() => {
    // Two skills so the menu has skill rows distinct from the built-ins.
    // Skills fill the textarea (with a trailing space) on selection rather
    // than executing, which lets us assert the completed value directly
    // without invoking store actions like compact().
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [
        { name: "deep-research", description: "Run a deep research sweep" },
        { name: "deslop", description: "Remove AI slop" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    setOmnigentHostConfig({});
  });

  it("highlights the first match as soon as the menu opens", () => {
    // /compact is native-wrapper-only (#1139); render a native session so it
    // appears as the first built-in and is the default highlight.
    render(<Composer {...composerProps({ isNativeWrapper: true })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    // Built-ins are inserted first, so "/compact" tops the list and is the
    // default highlight — the crux of the fix (was -1 / nothing selected).
    expect(activeRow()?.textContent).toContain("/compact");
  });

  it("Tab completes the highlighted skill into the textarea", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "/des" narrows to the "deslop" skill (built-ins don't match "des").
    fireEvent.change(ta, { target: { value: "/des" } });
    expect(activeRow()?.textContent).toContain("/deslop");

    fireEvent.keyDown(ta, { key: "Tab" });
    // Skills fill "/name " and keep focus so the user can append args.
    expect(ta.value).toBe("/deslop ");
  });

  it("Tab completes a match found only mid-name (exercises menuMatches, not just the render filter)", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "slop" is a substring of "deslop" but a prefix of no command. The menu
    // render filter would show the row either way; Tab-completion reads
    // menuMatches[menuIndex], so this only completes if the keyboard-nav
    // filter is substring-based. Guards menuMatches from silently reverting
    // to prefix matching and diverging from the rendered list.
    fireEvent.change(ta, { target: { value: "/slop" } });
    expect(activeRow()?.textContent).toContain("/deslop");
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/deslop ");
  });

  it("ranks a prefix built-in ahead of mid-string matches so a short query can't execute the wrong command", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // "/e": /effort is a prefix match; /context and /help merely contain "e".
    // Before prefix-priority ranking, /context (a no-arg builtin) was
    // highlighted first and Tab/Enter executed it — a side-effecting
    // regression. /effort must win and Tab fills it (it takes an argument).
    fireEvent.change(ta, { target: { value: "/e" } });
    expect(activeRow()?.textContent).toContain("/effort");
    fireEvent.keyDown(ta, { key: "Tab" });
    expect(ta.value).toBe("/effort ");
  });

  it("Enter completes the highlighted command instead of sending", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/des" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(ta.value).toBe("/deslop ");
    expect(onSend).not.toHaveBeenCalled();
    // Completion fills "/deslop " (trailing space) which closes the menu —
    // no row stays highlighted.
    expect(activeRow()).toBeNull();
  });

  it("Enter sends a normal (non-slash) message and reports the send to analytics", () => {
    const onSend = vi.fn();
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "hello there" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("hello there", undefined);
    // Enter-key sends must emit the same telemetry as clicking Send.
    expect(analytics).toHaveBeenCalledWith({
      type: "click",
      componentId: "chat.composer.send",
      componentKind: "button",
    });
  });

  it("Enter on an empty composer neither sends nor reports a send", () => {
    const onSend = vi.fn();
    const analytics = vi.fn();
    setOmnigentHostConfig({ analytics });
    render(<Composer {...composerProps({ onSend })} />);

    // Empty draft: the Send button is disabled, so a click can't fire the
    // event — the guarded Enter path must not fire it either.
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
    expect(analytics).not.toHaveBeenCalled();
  });

  it("does not send when Enter confirms active IME composition", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.compositionStart(ta);
    fireEvent.change(ta, { target: { value: "オムニジェント" } });

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.compositionEnd(ta);
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("オムニジェント", undefined);
  });

  it("does not send when Enter carries the IME keyCode 229 fallback", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "omnigent" } });

    fireEvent.keyDown(ta, { key: "Enter", keyCode: 229 });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("omnigent", undefined);
  });

  it("ArrowDown moves the highlight to the next match", () => {
    // /compact is native-wrapper-only (#1139); render a native session so the
    // first built-in is "/compact" and ArrowDown advances to "/context".
    render(<Composer {...composerProps({ isNativeWrapper: true })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/" } });
    expect(activeRow()?.textContent).toContain("/compact");

    fireEvent.keyDown(ta, { key: "ArrowDown" });
    // Second built-in entry.
    expect(activeRow()?.textContent).toContain("/context");
  });
});

describe("Composer slash-command submit routing", () => {
  // Several tests below swap the store's setModel for a vi.fn(); restore
  // the real action after each test so the mock can't bleed into later
  // tests in this file (zustand state is module-global).
  const realSetModel = useChatStore.getState().setModel;

  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [
        { name: "deep-research", description: "Run a deep research sweep" },
        { name: "deslop", description: "Remove AI slop" },
      ],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ setModel: realSetModel, sessionHarness: null });
  });

  it("routes a known skill through onSendSlashCommand with parsed args", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // Trailing text after the name → menu is closed (has a space), so Enter
    // submits rather than completing the menu. Name is sent without the
    // leading slash; everything after the first token is the argument text.
    fireEvent.change(ta, { target: { value: "/deslop fix the bug" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "fix the bug");
    // It's a slash_command event, NOT a plaintext message.
    expect(onSend).not.toHaveBeenCalled();
  });

  it("routes a known skill whose args carry slashes (paths, URLs)", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // The command guard checks only the "/deslop" token, so slashes in the
    // argument text (file paths, PR URLs) must not demote the send to
    // plaintext — the regression the review bot flagged on the landing
    // matcher applies here identically since both share isSlashCommandText.
    fireEvent.change(ta, { target: { value: "/deslop fix src/foo.ts" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "fix src/foo.ts");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("treats a path-shaped first token as plaintext, not a command", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // "/etc/hosts" has a "/" inside the first token — a file path. It must
    // fall through to the plaintext path, not error as an unknown command.
    fireEvent.change(ta, { target: { value: "/etc/hosts is broken" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/etc/hosts is broken", undefined);
  });

  it("sends empty arguments for a known skill with no args", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // Trailing space closes the menu so Enter submits the bare command.
    fireEvent.change(ta, { target: { value: "/deslop " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).toHaveBeenCalledWith("deslop", "");
    // Took the event path, not the plaintext fallback.
    expect(onSend).not.toHaveBeenCalled();
  });

  it("falls through to plaintext onSend for an unknown command", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand })} />);
    const ta = textarea();
    // No matching skill/builtin → not a slash_command; sent as a message.
    fireEvent.change(ta, { target: { value: "/not-a-real-skill" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/not-a-real-skill", undefined);
  });

  it("treats /effort as plaintext when effort controls are hidden", () => {
    const onSend = vi.fn();
    const onSendSlashCommand = vi.fn();
    render(<Composer {...composerProps({ onSend, onSendSlashCommand, showEffort: false })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/effort high" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSendSlashCommand).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/effort high", undefined);
  });

  it("native sessions (no onSendSlashCommand) send a known skill as plaintext", () => {
    // composerProps omits onSendSlashCommand — this models a native-terminal
    // session where the event path is disabled and the vendor TUI handles
    // the skill. The known skill must fall through to plaintext onSend.
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/deslop " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).toHaveBeenCalledWith("/deslop", undefined);
  });

  it("routes /model to setModel on in-process sessions (matches REPL /model)", () => {
    // isTerminalFirst defaults to false → showModel true. The command must
    // write the override via setModel (NOT send the literal "/model …" text
    // to the agent) so the next turn runs on the new model. The visible
    // confirmation is the server-appended `[System: model changed…]`
    // transcript note, not inline composer text — so nothing to assert here
    // beyond the routing.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();
    // Space closes the menu so Enter submits; bare gateway id has no "/".
    fireEvent.change(ta, { target: { value: "/model databricks-gpt-5-4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("databricks-gpt-5-4", {
      expectConfirmation: false,
    });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("clears the override for /model default|off|reset", () => {
    // The REPL clear aliases map to setModel(null) → server "default"
    // sentinel. A wrong value here (e.g. the literal "default" string)
    // would pin a bogus model instead of restoring the agent default.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model default" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith(null, { expectConfirmation: false });
  });

  it("treats /model as plaintext on native-wrapper sessions without a model picker", () => {
    // isNativeWrapper without showModels → showModel false: native wrappers
    // need an explicit picker-backed propagation path. Without one, /model
    // must NOT fire setModel — it falls through to a plaintext message.
    // Terminal-first SDK sessions (embedded Omnigent REPL terminal) keep the
    // in-process routing.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(
      <Composer {...composerProps({ onSend, isTerminalFirst: true, isNativeWrapper: true })} />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model databricks-gpt-5-4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("/model databricks-gpt-5-4", undefined);
  });

  it("opens the primary model picker for bare /model when the picker is available", async () => {
    // claude-native (showModels): a plaintext "/model" would open Claude's
    // interactive selector inside the vendor TUI, which the web UI can't
    // render — the session just blocks. The composer must intercept the
    // bare command and open the config gear modal (which owns the Model
    // dropdown) instead of sending.
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    expect(ta.value).toBe("");
    // The config modal is open with the Model control to choose from.
    expect(await screen.findByTestId("composer-agent-config-menu")).toBeTruthy();
    expect(screen.getByTestId("composer-agent-models")).toBeTruthy();
  });

  it("shows one merged tooltip on the pill, carrying the model connection", async () => {
    useChatStore.setState({ llmModel: "sonnet" });
    const options = CLAUDE_MODEL_OPTIONS.map((option) => ({
      ...option,
      source: {
        kind: "databricks",
        label: "Workspace",
        name: "production-west",
        host: "acme.cloud.databricks.com",
      },
    }));
    render(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    const pill = screen.getByTestId("composer-config-gear");
    expect(pill).toHaveTextContent("Sonnet 4.6");
    expect(pill).not.toHaveTextContent("Workspace");
    fireEvent.focus(pill);
    const gearTooltip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(gearTooltip).toHaveTextContent("Connection: Databricks · production-west");
    expect(gearTooltip).not.toHaveTextContent("acme.cloud.databricks.com");
    expect(gearTooltip).not.toHaveTextContent("Profile:");
    expect(gearTooltip).not.toHaveTextContent("Host:");
    expect(gearTooltip.textContent?.indexOf("Connection:")).toBeGreaterThan(
      gearTooltip.textContent?.indexOf("Effort:") ?? -1,
    );
    // Bold keys separate each row's label from its value.
    for (const key of within(gearTooltip).getAllByText(/^(Harness|Model|Effort|Connection):$/)) {
      expect(key).toHaveClass("font-semibold");
    }
    // The pill owns exactly one tooltip surface — a second wrapper surface
    // (the old model-source tooltip) stacked over it is the reported bug.
    expect(screen.queryByTestId("composer-model-source-tooltip")).toBeNull();
    expect(document.querySelectorAll('[data-slot="tooltip-content"]')).toHaveLength(1);
  });

  it("suppresses the pill tooltip when bare /model opens the picker", async () => {
    // The programmatic openNonce path (bare `/model`) must suppress the
    // pill's summary tooltip exactly like the click/keyboard open paths;
    // otherwise the focus the gear receives (inside the tooltip trigger's
    // span, so it bubbles there) paints the tooltip over the just-opened
    // selector.
    useChatStore.setState({ llmModel: "sonnet" });
    render(
      <Composer
        {...composerProps({
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });
    await screen.findByTestId("composer-agent-menu");

    fireEvent.focus(screen.getByTestId("composer-config-gear"));
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 25);
        }),
    );
    expect(screen.queryByTestId("composer-config-gear-tooltip")).toBeNull();
  });

  it("suppresses the pill tooltip while the selector popover is open", async () => {
    useChatStore.setState({ llmModel: "sonnet" });
    render(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );

    // Open the selector popover from the pill.
    const gear = screen.getByTestId("composer-config-gear");
    fireEvent.keyDown(gear, { key: "ArrowDown" });
    const menu = screen.getByTestId("composer-agent-menu");

    // Opening the menu focuses the gear, which sits inside the tooltip
    // trigger's span and bubbles to it (the menu content is a portalled
    // React sibling — its events do not bubble to the trigger); the tooltip
    // must stay closed rather than paint over the open menu.
    fireEvent.focus(gear);
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 25);
        }),
    );
    expect(screen.queryByTestId("composer-config-gear-tooltip")).toBeNull();

    // Closing the popover hands focus back to the trigger; that programmatic
    // focus must NOT instantly reopen the tooltip.
    fireEvent.keyDown(menu, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("composer-agent-menu")).toBeNull());
    fireEvent.focus(gear);
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 25);
        }),
    );
    expect(screen.queryByTestId("composer-config-gear-tooltip")).toBeNull();

    // Fresh intent releases the suppression: the pointer re-entering the
    // tooltip trigger (the span wrapping the pill, which carries the
    // guard's pointer handler) lets the tooltip open again.
    fireEvent.pointerEnter(gear.parentElement!);
    fireEvent.focus(gear);
    expect(await screen.findByTestId("composer-config-gear-tooltip")).toBeInTheDocument();
  });

  it("keeps the label's truncation chain intact through the pill's wrapper", () => {
    useChatStore.setState({ llmModel: "sonnet" });
    const options = CLAUDE_MODEL_OPTIONS.map((option) => ({
      ...option,
      source: {
        kind: "databricks",
        label: "Workspace",
        name: "production-west",
        host: "acme.cloud.databricks.com",
      },
    }));
    render(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    // jsdom does no layout, so pin the CSS contract instead: the pill's
    // tooltip-trigger wrapper must be a shrinkable flex container (flex +
    // min-w-0), or the label's `truncate` never engages and a long model id
    // runs under the Stop button on phone-width viewports.
    const wrapper = screen.getByTestId("composer-config-gear").parentElement as HTMLElement;
    for (const cls of ["flex", "min-w-0"]) {
      expect(wrapper.classList.contains(cls), `wrapper is missing "${cls}"`).toBe(true);
    }
    const label = screen.getByTestId("composer-agent-config-value");
    for (const cls of ["inline-flex", "min-w-0"]) {
      expect(label.classList.contains(cls), `label is missing "${cls}"`).toBe(true);
    }
  });

  it("labels subscription provenance instead of showing an unexplained CLI name", async () => {
    useChatStore.setState({ llmModel: "sonnet" });
    const options = CLAUDE_MODEL_OPTIONS.map((option) => ({
      ...option,
      source: { kind: "subscription", label: "Subscription", name: "claude" },
    }));
    render(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    fireEvent.focus(screen.getByTestId("composer-config-gear"));
    const tooltip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tooltip).toHaveTextContent("Connection: Claude subscription");
    expect(tooltip).not.toHaveTextContent("Authentication");
  });

  it("does not open an empty Claude config modal while the live catalog loads", () => {
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: [],
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(onSend).not.toHaveBeenCalled();
    // No catalog yet → fall through to the read-only hint, not an empty modal.
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
    expect(screen.getByText(/Usage: \/model <name>/)).toBeVisible();
  });

  it("opens the primary model picker for bare /model on opencode-native", async () => {
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      setModel,
      llmModel: "opencode-go/glm-5.2",
    });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "opencode",
          codexModelOptions: [{ id: "opencode-go/glm-5.2", displayName: "opencode-go/glm-5.2" }],
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model " } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // Bare /model opens the modal without sending text or changing the model.
    expect(onSend).not.toHaveBeenCalled();
    expect(setModel).not.toHaveBeenCalled();
    expect(await screen.findByTestId("composer-agent-config-menu")).toBeTruthy();
    expect(screen.getByTestId("composer-agent-models")).toBeTruthy();
  });

  it("routes /model <name> to setModel on opencode-native (functional switch)", () => {
    // Even with an empty picker list, "/model <name>" must persist the override
    // via setModel — the opencode executor reads model_override on the next
    // web-injected turn. It must NOT leak to the agent as plaintext "/model …".
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "opencode",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model openrouter/llama-3.3-70b" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("openrouter/llama-3.3-70b", {
      expectConfirmation: false,
    });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("routes /model <name> to setModel on claude-native sessions", () => {
    // Sent as plaintext, "/model fable" would pop Claude's "Switch model?"
    // dialog inside the vendor TUI with nothing web-side to answer it —
    // the session just blocks. The command must take the picker's path
    // instead: setModel persists the override and the runner injects
    // "/model <name>" into the pane with auto-confirm.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel, sessionHarness: "claude-native" });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model fable" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    // Reported-model session: the ask is marked pending until the
    // harness's own report (or the not-applied error) settles it.
    expect(setModel).toHaveBeenCalledWith("fable", { expectConfirmation: true });
    expect(onSend).not.toHaveBeenCalled();
    // The config modal only opens for the bare command, not the argument form.
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("routes /model <name> to setModel on codex-native sessions", () => {
    // Codex-native propagates the persisted override via Codex app-server
    // `thread/settings/update`, so it follows the same picker-backed route
    // as claude-native instead of sending plaintext into the terminal.
    const setModel = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setModel, sessionHarness: "codex-native" });
    const onSend = vi.fn();
    render(
      <Composer
        {...composerProps({
          onSend,
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "codex",
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/model gpt-5.4" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(setModel).toHaveBeenCalledWith("gpt-5.4", { expectConfirmation: true });
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("Composer model/effort label", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      // Reset the per-session override too: a test that sets it must not leak
      // into the next, which now reads sessionModelOverride first for the label.
      sessionModelOverride: null,
      codexModelOptions: [],
      nativeVendorOwnsModel: false,
      // Identity-fallback inputs: reset so a case that sets one can't leak it
      // into the next (the label reads both when no model/effort resolves).
      sessionHarness: null,
      subAgentName: null,
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const label = () => screen.getByTestId("composer-agent-config-value");

  it("shows the catalog display name beside Edit in the shared harness row", () => {
    useChatStore.setState({
      llmModel: "system.ai.claude-opus-4-6",
      sessionHarness: "claude-native",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: [
            { id: "opus", model: "system.ai.claude-opus-4-6", displayName: "Opus" },
          ],
        })}
      />,
    );
    expect(label()).toHaveTextContent("Opus");
    fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
    const row = screen.getByTestId("composer-agent-edit");
    expect(row).toHaveClass("composer-agent-row");
    expect(row).toHaveAttribute("data-active", "true");
    expect(within(row).getByText("Opus")).toHaveClass("text-right");
    expect(within(row).getByText("Edit")).toHaveClass("composer-agent-edit");
    fireEvent.keyDown(row, { key: "ArrowRight" });
    expect(screen.getByTestId("composer-agent-model-opus")).toHaveTextContent("Opus");
  });

  it("shows the model in the foreground and effort muted", () => {
    // The chip renders the harness's reported model (`llmModel`), never the
    // sticky preference or the request.
    useChatStore.setState({ llmModel: "opus", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Opus");
    expect(label()).toHaveTextContent("High");
    // The harness identity ("Claude") is NOT in the label — it lives in the gear tooltip.
    expect(label()).not.toHaveTextContent("Claude");
    // Model black, effort grey.
    expect(within(label()).getByText("Opus")).toHaveClass("text-foreground");
    expect(within(label()).getByText("High")).toHaveClass("text-muted-foreground");
  });

  it("shows no effort for a seeded null, never borrowing the cross-session sticky (#7039)", () => {
    // Same sticky "high" as the test above, but the optimistic create seeded an
    // intentional "no effort" (sessionEffortSeeded) — the authoritative seed
    // must win, so the label shows the model with no effort, not "High".
    useChatStore.setState({
      llmModel: "opus",
      selectedEffort: "high",
      sessionReasoningEffort: null,
      sessionEffortSeeded: true,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Opus");
    expect(label()).not.toHaveTextContent("High");
    expect(screen.queryByTestId("composer-agent-effort-value")).toBeNull();
    // This suite shares one global store and only resets what each test sets;
    // no other test touches the seeded flag, so restore the default here.
    useChatStore.setState({ sessionEffortSeeded: false });
  });

  it("reads 'Smart Routing' with no model/effort when routing is on", async () => {
    // The router picks model + effort per turn, so the label must not surface a
    // stale pinned model/effort — it reads "Smart Routing" instead.
    useChatStore.setState({
      selectedModel: "opus",
      selectedEffort: "high",
      costControlModeOverride: "on",
    });
    const options = CLAUDE_MODEL_OPTIONS.map((option) => ({
      ...option,
      source: { kind: "subscription", label: "Subscription", name: "claude" },
    }));
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          costRoutingEligible: true,
          codexModelOptions: options,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Smart Routing");
    expect(label()).not.toHaveTextContent("Opus");
    expect(label()).not.toHaveTextContent("High");
    // Routing picks the connection per turn, so the pill tooltip must not
    // surface a stale pinned provenance row.
    fireEvent.focus(screen.getByTestId("composer-config-gear"));
    const gearTooltip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(gearTooltip).not.toHaveTextContent("Connection:");
  });

  it("renders the reported model, never the request or the sticky", () => {
    useChatStore.setState({
      selectedModel: "opus",
      sessionModelOverride: "sonnet",
      selectedEffort: null,
      llmModel: "haiku",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    // The harness's report ("haiku") is the display authority: neither the
    // pending request ("sonnet") nor the cross-session sticky ("opus") may
    // render as if it were the session's model.
    expect(label()).toHaveTextContent("Haiku");
    expect(label()).not.toHaveTextContent("Opus");
    expect(label()).not.toHaveTextContent("Sonnet 4.6");
  });

  const CLAUDE_LIVE_OPTIONS = [
    { id: "opus", model: "system.ai.claude-opus-4-10", displayName: "Opus 4.10", isDefault: false },
    { id: "sonnet", model: "system.ai.claude-sonnet-5", displayName: "Sonnet 5", isDefault: true },
  ];

  it("uses the catalog display name for an exact Claude model ID in the read-only label", () => {
    useChatStore.setState({
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: "system.ai.claude-sonnet-5",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_LIVE_OPTIONS,
        })}
      />,
    );

    expect(label()).toHaveTextContent("Sonnet 5");
    expect(label()).not.toHaveTextContent("system.ai.claude-sonnet-5");
    // The modal's catalog-default fallback (isDefault row when no concrete
    // model) is exercised in the real browser by the claude model-picker e2e
    // tests, where the Radix Select actually mounts its option rows.
  });

  it("keeps the harness visible when model and effort are unresolved", () => {
    // A claude-native session before the snapshot fills llmModel/selectedEffort
    // has no model label and no effort label. The read-only label renders
    // nothing rather than a placeholder — the gear still owns the config path.
    useChatStore.setState({ selectedModel: null, selectedEffort: null, llmModel: null });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(screen.getByTestId("composer-agent-config-value")).toHaveTextContent("Claude Code");
    // The gear is still present so the user can open the config modal.
    expect(screen.getByTestId("composer-config-gear")).toBeTruthy();
  });

  it("falls back to the harness identity for an SDK/bundle agent with no model/effort", () => {
    // Polly (claude-sdk/pi bundle) surfaces no model or effort, so the label
    // would be empty. It falls back to the harness identity ("Polly (Pi)") so
    // the slot isn't blank.
    useChatStore.setState({
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      sessionHarness: "pi",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
          showModels: false,
          showEffort: false,
        })}
      />,
    );
    expect(label()).toHaveTextContent("Polly (Pi)");
  });

  it("names the vendor, not the Task subagent_type, on a Claude Code sub-agent", () => {
    // A claude-native sub-agent child has no model of its own, so the label
    // takes the identity fallback. Its `subAgentName` is Claude's own
    // `subagent_type` ("general-purpose") and it reuses the parent's
    // claude-native agent row — neither names the product, so the wrapper
    // label decides. The instance itself is named in the sub-agent tray.
    useChatStore.setState({
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      sessionHarness: "claude-native",
      subAgentName: "general-purpose",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude-native-ui" }],
          selectedAgentId: "a1",
          // No picker: the sub-agent is read-only, so it has no model control.
          modelPickerKind: null,
          showModels: false,
          showEffort: false,
          wrapperLabel: "claude-code-native-ui-subagent",
          readOnlyReason: "Claude Code sub-agents are read-only",
        })}
      />,
    );
    expect(label()).toHaveTextContent("Claude Code");
    expect(label()).not.toHaveTextContent("General-purpose");
  });

  it("does NOT fall back to the bare vendor name for a native wrapper with no model", () => {
    // A native wrapper's harnessLabel is the bare vendor name ("Claude"), which
    // the gear tooltip owns now — the label must stay empty when unresolved
    // rather than resurrecting it. Only SDK/bundle agents get the fallback.
    useChatStore.setState({ selectedModel: null, selectedEffort: null, llmModel: null });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          showEffort: false,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    expect(screen.getByTestId("composer-agent-config-value")).toHaveTextContent("Claude Code");
  });

  it("surfaces a cursor-native session's model from the override, not the cross-session sticky", () => {
    // cursor-native is a vendor-owns-model wrapper, so `nativeVendorOwnsModel`
    // is true and the bound `llmModel` is a meaningless default. Its live model
    // is mirrored into the session override (`sessionModelOverride`), NOT the
    // cross-session sticky `selectedModel`. The label must read the real
    // session model ("Composer 2.5"), not the stale sticky pick carried over
    // from another session.
    useChatStore.setState({
      nativeVendorOwnsModel: true,
      selectedModel: "opus-4.5", // stale cross-session sticky — must be ignored
      sessionModelOverride: "composer-2.5",
      selectedEffort: "low",
      llmModel: "fable", // meaningless vendor default — must not surface
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "cursor" }],
          selectedAgentId: "a1",
          modelPickerKind: "cursor",
          showModels: true,
          showEffort: false, // cursor effort control is dropped for now
          codexModelOptions: [
            { id: "composer-2.5", displayName: "Composer 2.5" },
            { id: "opus-4.5", displayName: "Opus 4.5" },
          ],
        })}
      />,
    );
    expect(label()).toHaveTextContent("Composer 2.5");
    // Neither the stale sticky, the meaningless vendor default, nor any effort leaks in.
    expect(label()).not.toHaveTextContent("Opus 4.5");
    expect(label()).not.toHaveTextContent("fable");
    expect(label()).not.toHaveTextContent("Low");
    expect(within(label()).getByText("Composer 2.5")).toHaveClass("text-foreground");
  });

  it("surfaces an SDK/bundle session's model from the override, not the cross-session sticky", () => {
    // Polly/Debby (claude-sdk) repro: a model picked in some other (Codex)
    // session lingers in the global sticky `selectedModel`. SDK/bundle sessions
    // (modelPickerKind === null) never have the sticky applied, so the label
    // must read the session's own applied model (`sessionModelOverride`), never
    // the stale sticky — the "gpt-5.5 on a Claude-SDK Polly" report.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // stale cross-session sticky — must be ignored
      sessionModelOverride: "claude-opus-4-8",
      selectedEffort: null,
      llmModel: null,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
        })}
      />,
    );
    expect(label()).toHaveTextContent("claude-opus-4-8");
    expect(label()).not.toHaveTextContent("gpt-5.5");
  });

  it("does not leak the cross-session sticky model on an SDK/bundle session with no applied model", () => {
    // The exact report: a Polly (claude-sdk) session with no override and no
    // bound model, but a `gpt-5.5` left in the sticky from a prior Codex
    // session. The model label stays empty — only the real effort shows.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // stale cross-session sticky — must not surface
      sessionModelOverride: null,
      selectedEffort: "high",
      llmModel: null,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
        })}
      />,
    );
    expect(label()).not.toHaveTextContent("gpt-5.5");
    // The real effort still renders — proving the label is present and only
    // the leaked model was suppressed.
    expect(label()).toHaveTextContent("High");
  });

  it("does not leak the cross-session sticky model on a native session before its catalog lands", () => {
    // The Codex→Claude switch repro: `switchTo` clears the session-scoped model
    // fields but keeps the sticky, so mid-switch a claude-native session has an
    // empty catalog and the `gpt-5.5` the outgoing Codex session left behind.
    // The label must wait for a model this session vouches for rather than
    // paint the previous session's pick for the whole bind round trip.
    useChatStore.setState({
      selectedModel: "gpt-5.5", // outgoing Codex session's pick — must not surface
      sessionModelOverride: null,
      selectedEffort: "high",
      llmModel: null,
      codexModelOptions: [], // cleared by `switchTo`, refilled when the snapshot lands
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: [],
        })}
      />,
    );
    expect(label()).not.toHaveTextContent("gpt-5.5");
    // The real effort still renders — only the leaked model was suppressed.
    expect(label()).toHaveTextContent("High");
  });

  it("opens model configuration from Edit in the shared picker", async () => {
    // The pill-wide hover highlight advertises one clickable control, so the
    // label half must perform the same action as the gear beside it.
    useChatStore.setState({ llmModel: "opus", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );

    openSessionConfig();

    fireEvent.click(screen.getByTestId("composer-agent-edit"));
    expect(await screen.findByTestId("composer-agent-config-menu")).toBeTruthy();
  });

  it("keeps the label click inert when the session is read-only", () => {
    // Mirrors the gear's soft-disable: a read-only viewer gets no config modal
    // from either pill half, and aria-disabled drops the pill's hover
    // highlight so no dead affordance is advertised.
    useChatStore.setState({ llmModel: "opus", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "claude" }],
          selectedAgentId: "a1",
          modelPickerKind: "claude",
          showModels: true,
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
          readOnlyReason: "Mirrored transcript",
        })}
      />,
    );

    expect(screen.getByTestId("composer-config-gear")).toHaveAttribute("aria-disabled", "true");
    openSessionConfig();
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("keeps the harness identity visible in a disabled shared trigger", () => {
    // SDK/bundle identity fallback with no config surface: there's no modal
    // to open, no button in the pill, and thus no hover highlight to honor.
    useChatStore.setState({
      selectedModel: null,
      selectedEffort: null,
      llmModel: null,
      sessionHarness: "pi",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          agents: [{ id: "a1", name: "polly" }],
          selectedAgentId: "a1",
          modelPickerKind: null,
          showModels: false,
          showEffort: false,
        })}
      />,
    );

    expect(screen.getByTestId("composer-config-gear")).toBeDisabled();
    expect(label().tagName).toBe("SPAN");
  });
});

describe("Composer shared visible controls", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  // The workspace/worktree popover markup moved into the shared
  // ComposerWorkspaceStatus component (its own tests cover the popover text
  // wrapping); the two page-local inline-dropdown popover cases retired with it.

  it("renders the same workspace, host, permission and model controls as landing", () => {
    useChatStore.setState({
      conversationId: "shared-controls",
      sessionHarness: "codex-native",
      gitBranch: "feature/shared-composer",
      codexApprovalMode: "ask-for-approval",
      llmModel: null,
      selectedEffort: null,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showCodexApprovalMode: true,
          modelPickerKind: "codex",
          showModels: true,
        })}
      />,
    );
    const workspace = screen.getByTestId("composer-workspace-controls");
    const card = textarea().closest("[data-composer-card]");
    const actions = screen.getByTestId("composer-action-row");
    expect(textarea().parentElement?.parentElement).toBe(card);
    expect(actions.parentElement).toBe(card);
    const [widthProbe, leading, trailing] = Array.from(actions.children);
    expect(widthProbe).toHaveClass("h-0");
    expect(leading).toContainElement(screen.getByRole("button", { name: "Add" }));
    expect(trailing).toContainElement(screen.getByTestId("composer-config-gear"));
    expect(actions.children).toHaveLength(3);
    expect(workspace).toHaveClass("mx-3", "h-[37px]", "rounded-t-2xl");
    // The branch text now flows through the shared ComposerWorkspaceStatus +
    // useComposerGitStatus (covered by their own tests); here assert the shared
    // branch control renders in the bar.
    expect(within(workspace).getByTestId("composer-git-branch")).toBeInTheDocument();
    expect(screen.getByTestId("composer-host-select")).toHaveClass("w-11", "md:h-7");
    expect(screen.getByTestId("composer-permission-chip")).toHaveTextContent("Ask for approval");
    const trigger = screen.getByTestId("composer-config-gear");
    expect(trigger.querySelector("img")).toBeTruthy();
    expect(trigger.querySelector(".lucide-settings")).toBeNull();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getByTestId("composer-agent-menu")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("passes the real session id/host/workspace/creation-branch to useComposerGitStatus", () => {
    // The workspace bar is fed by the page adapter, not fixtures: assert the
    // page threads the actual session identity through useComposerGitStatus.
    composerGitStatusArgsSpy.mockClear();
    useChatStore.setState({ conversationId: "conv_git_args", gitBranch: "feature/x" });
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(composerGitStatusArgsSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        sessionId: "conv_git_args",
        creationBranch: "feature/x",
        hostId: null,
        workspace: null,
      }),
    );
  });

  it("dispatches the shared permission picker to the session setter", async () => {
    useChatStore.setState({
      conversationId: "shared-permissions",
      codexApprovalMode: "ask-for-approval",
    });
    const setApproval = vi
      .spyOn(useChatStore.getState(), "setCodexApprovalMode")
      .mockResolvedValue(undefined);
    renderWithTooltips(<Composer {...composerProps({ showCodexApprovalMode: true })} />);
    fireEvent.keyDown(screen.getByTestId("composer-permission-chip"), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-permission-option-read-only"));
    await waitFor(() => expect(setApproval).toHaveBeenCalledWith("read-only"));
  });
});

describe("Composer effort slash-command visibility", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("omits /effort from suggestions when effort controls are hidden", () => {
    // /compact is native-wrapper-only (#1139); render a native session so it
    // stays present as the control row used to anchor this assertion.
    render(<Composer {...composerProps({ showEffort: false, isNativeWrapper: true })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });

    // Row testids — /compact is hidden for non-native-wrapper sessions,
    // so verify /context is present instead.
    expect(screen.queryByTestId("slash-menu-item-effort")).toBeNull();
    expect(screen.getByTestId("slash-menu-item-context")).toBeInTheDocument();
  });

  it("shows /compact for a claude-sdk session", () => {
    // claude-sdk is not a native wrapper, but its runner sends /compact to
    // the live SDK client to trigger native compaction, so the command is
    // offered even though isNativeWrapper is false.
    useChatStore.setState({ sessionHarness: "claude-sdk" });
    render(<Composer {...composerProps({ isNativeWrapper: false })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-compact")).toBeInTheDocument();
  });

  it("hides /compact for a non-native, non-claude-sdk session", () => {
    // Other in-process SDK harnesses (openai-agents) have no /compact path
    // yet, so the command stays hidden.
    useChatStore.setState({ sessionHarness: "openai-agents" });
    render(<Composer {...composerProps({ isNativeWrapper: false })} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.queryByTestId("slash-menu-item-compact")).toBeNull();
  });

  it("shows /model in suggestions for in-process and picker-backed native sessions", () => {
    // Type just "/" (like the /effort case) so the highlight overlay shows
    // only "/" — keeps the menu row the sole "/model" match.
    // Default (isTerminalFirst false) → /model offered.
    const { unmount } = render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
    unmount();

    // Terminal-first SDK session (embedded Omnigent REPL terminal, no
    // native wrapper) → still an in-process harness, /model stays offered.
    const { unmount: unmountSdk } = render(
      <Composer {...composerProps({ isTerminalFirst: true })} />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByText("/model")).toBeInTheDocument();
    unmountSdk();

    // Native wrapper without the model picker → /model suppressed.
    const { unmount: unmountNativeNoPicker } = render(
      <Composer {...composerProps({ isTerminalFirst: true, isNativeWrapper: true })} />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.queryByTestId("slash-menu-item-model")).toBeNull();
    unmountNativeNoPicker();

    // claude-native and codex-native (wrapper WITH the model picker) →
    // /model offered; it routes to setModel so the override propagates via
    // the runner.
    const { unmount: unmountClaude } = render(
      <Composer
        {...composerProps({
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
        })}
      />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
    unmountClaude();

    render(
      <Composer
        {...composerProps({
          isTerminalFirst: true,
          isNativeWrapper: true,
          showModels: true,
          modelPickerKind: "codex",
        })}
      />,
    );
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByTestId("slash-menu-item-model")).toBeInTheDocument();
  });
});

describe("Composer Codex Plan-mode control", () => {
  const realSetCodexPlanMode = useChatStore.getState().setCodexPlanMode;

  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      codexPlanMode: false,
      skills: [],
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ setCodexPlanMode: realSetCodexPlanMode, codexPlanMode: false });
  });

  it("toggles Codex Plan mode through the store action", async () => {
    const setCodexPlanMode = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({ setCodexPlanMode });

    renderWithTooltips(<Composer {...composerProps({ showCodexPlanMode: true })} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    fireEvent.click(screen.getByTestId("composer-plan-action"));

    await waitFor(() => expect(setCodexPlanMode).toHaveBeenCalledWith(true));
  });

  it("shows active Plan mode in the shared action menu", () => {
    useChatStore.setState({ codexPlanMode: true });

    renderWithTooltips(<Composer {...composerProps({ showCodexPlanMode: true })} />);

    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    const button = screen.getByTestId("composer-plan-action");
    expect(button).toHaveAttribute("data-active", "true");
    expect(button).toHaveAccessibleName("Exit Plan mode");
  });

  it("keeps Plan in the shared menu instead of a separate toolbar button", () => {
    renderWithTooltips(<Composer {...composerProps({ showCodexPlanMode: true })} />);

    expect(screen.getByTestId("composer-action-row")).toHaveClass("@container/composer-actions");
    expect(screen.queryByRole("button", { name: "Enter Plan mode" })).toBeNull();
    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    expect(screen.getByRole("menuitem", { name: "Enter Plan mode" })).toBeInTheDocument();
  });

  it("hides the control when the session is not Codex-native", () => {
    render(<Composer {...composerProps({ showCodexPlanMode: false })} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Add" }), { key: "ArrowDown" });
    expect(screen.queryByTestId("composer-plan-action")).toBeNull();
  });
});

describe("slashCommandMatches", () => {
  it("matches the leaf segment after a namespace prefix", () => {
    expect(slashCommandMatches("/superpowers:using-superpowers", "using-superpowers")).toBe(true);
  });

  it("matches a substring in the middle of the name", () => {
    expect(slashCommandMatches("/cross-review", "rev")).toBe(true);
  });

  it("does not match a word that only appears in the description", () => {
    // Matching is name-only — the web menu never shows descriptions inline,
    // so a description-driven hit would look unexplained. "window" is in this
    // command's blurb but not its name, so it must NOT match.
    expect(slashCommandMatches("/context", "window")).toBe(false);
  });

  it("is case-insensitive on both name and query", () => {
    expect(slashCommandMatches("/Superpowers:Using", "USING")).toBe(true);
  });

  it("returns false when the query is nowhere in the name", () => {
    expect(slashCommandMatches("/context", "zzz")).toBe(false);
  });
});

describe("rankedSlashCommandNames", () => {
  it("ranks a prefix match ahead of commands that merely contain the query", () => {
    // "/e": /effort is a prefix; /context, /model, /help only contain "e".
    // Prefix-priority keeps /effort first so its auto-highlight + Enter can't
    // execute an unrelated no-arg builtin (/context) as a side effect.
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "e")[0]).toBe("/effort");
  });

  it("ranks /model ahead of commands that merely contain 'm'", () => {
    // "/m": /model is a prefix; /compact contains "m". Was /compact first.
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "m")[0]).toBe("/model");
  });

  it("keeps built-ins ahead of skills so the Commands section stays on top", () => {
    const commands = { ...BUILTIN_SLASH_COMMANDS, "/superpowers:effort-helper": "x" };
    const ranked = rankedSlashCommandNames(commands, "effort");
    // Both /effort (builtin, prefix) and the skill (mid-string) match; the
    // builtin must rank first so the render partition stays contiguous.
    expect(ranked[0]).toBe("/effort");
    expect(ranked.indexOf("/effort")).toBeLessThan(ranked.indexOf("/superpowers:effort-helper"));
  });

  it("ranks a prefix skill ahead of a mid-string skill, stably", () => {
    // Insertion order is deep-research, research; ranking promotes the prefix
    // match (research) above the mid-string one (deep-research contains "res").
    const commands = { "/deep-research": "a", "/research": "b" };
    expect(rankedSlashCommandNames(commands, "res")).toEqual(["/research", "/deep-research"]);
  });

  it("returns everything in insertion order for an empty query (lone '/')", () => {
    expect(rankedSlashCommandNames(BUILTIN_SLASH_COMMANDS, "")).toEqual(
      Object.keys(BUILTIN_SLASH_COMMANDS),
    );
  });
});

describe("SlashCommandMenu", () => {
  const COMMANDS = {
    "/alpha": "First",
    "/beta": "Second",
    "/gamma": "Third",
  };

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("marks the row at activeIndex as active", () => {
    render(<SlashCommandMenu query="" activeIndex={1} onSelect={vi.fn()} commands={COMMANDS} />);
    expect(activeRow()?.textContent).toContain("/beta");
  });

  it("scrolls the highlighted row into view when activeIndex changes", () => {
    const scrollSpy = vi.spyOn(Element.prototype, "scrollIntoView");
    const { rerender } = render(
      <SlashCommandMenu query="" activeIndex={0} onSelect={vi.fn()} commands={COMMANDS} />,
    );
    scrollSpy.mockClear();

    rerender(<SlashCommandMenu query="" activeIndex={2} onSelect={vi.fn()} commands={COMMANDS} />);
    // The effect keeps the keyboard selection visible as it scrolls past the
    // capped-height list; "nearest" avoids yanking the whole page.
    expect(scrollSpy).toHaveBeenCalledWith({ block: "nearest" });

    // The effect is keyed on activeIndex — a re-render that doesn't move the
    // selection must not re-scroll (otherwise unrelated re-renders would yank
    // the list around). Proves the [activeIndex] dependency, not "fires every
    // render".
    scrollSpy.mockClear();
    rerender(<SlashCommandMenu query="" activeIndex={2} onSelect={vi.fn()} commands={COMMANDS} />);
    expect(scrollSpy).not.toHaveBeenCalled();
  });

  it("filters rows by the typed query", () => {
    render(<SlashCommandMenu query="be" activeIndex={0} onSelect={vi.fn()} commands={COMMANDS} />);
    // Row testids (not text) — the active entry's name also appears in the
    // detail card beside the panel, so a text query would double-match.
    expect(screen.getByTestId("slash-menu-item-beta")).toBeDefined();
    expect(screen.queryByTestId("slash-menu-item-alpha")).toBeNull();
    expect(screen.queryByTestId("slash-menu-item-gamma")).toBeNull();
  });

  it("invokes onSelect with the command name when a row is clicked", () => {
    const onSelect = vi.fn();
    render(<SlashCommandMenu query="" activeIndex={0} onSelect={onSelect} commands={COMMANDS} />);
    fireEvent.click(screen.getByTestId("slash-menu-item-gamma"));
    expect(onSelect).toHaveBeenCalledWith("/gamma");
  });

  it("shows the highlighted entry's description in the detail card", () => {
    render(<SlashCommandMenu query="" activeIndex={1} onSelect={vi.fn()} commands={COMMANDS} />);
    // Descriptions moved off the rows into the Cursor-style detail card:
    // only the active entry's blurb renders, next to the panel. If the
    // card regressed (or showed the wrong entry), users would lose the
    // only place a skill's description is visible.
    const detail = screen.getByTestId("slash-menu-detail");
    expect(detail.textContent).toContain("/beta");
    expect(detail.textContent).toContain("Second");
    expect(detail.textContent).not.toContain("First");
  });

  it("surfaces a namespaced skill by its leaf name", () => {
    render(
      <SlashCommandMenu
        query="using-superpowers"
        activeIndex={0}
        onSelect={vi.fn()}
        commands={{ "/superpowers:using-superpowers": "Establishes how to find and use skills" }}
      />,
    );
    expect(screen.getByTestId("slash-menu-item-superpowers:using-superpowers")).toBeDefined();
  });
});

// Renders the real composer and inspects the highlight overlay's DOM, so a
// regression where the WHOLE draft tints (not just the token) is caught.
describe("Composer slash-command highlight overlay", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });
  afterEach(() => cleanup());

  /** The only tinted (pink) run in the overlay — should be just the token. */
  function tintedText(): string | null {
    return (
      screen.getByTestId("composer-highlight-overlay").querySelector(".text-brand-accent")
        ?.textContent ?? null
    );
  }

  /** The overlay's full text, tinted + untinted — should mirror the draft. */
  function overlayText(): string {
    return screen.getByTestId("composer-highlight-overlay").textContent ?? "";
  }

  // A slash command followed by args; only the leading token should tint.
  const COMMAND_PROMPT =
    "/cross-review have Claude Code implement GH issue #<number>, then have Codex review";

  it("tints only the token for a command with args (args stay default)", () => {
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: COMMAND_PROMPT } });
    expect(textarea().value).toBe(COMMAND_PROMPT);
    expect(tintedText()).toBe("/cross-review");
    expect(overlayText()).toBe(COMMAND_PROMPT);
    expect(textarea()).toHaveClass("text-ui");
    expect(screen.getByTestId("composer-highlight-overlay")).toHaveClass("text-ui");
  });

  it("renders no overlay for plain prose", () => {
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "just a normal message" } });
    expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();
  });
});

describe("Composer placeholder", () => {
  afterEach(cleanup);

  it("shows the normal placeholder when the runner is live", () => {
    render(<Composer {...composerProps({})} />);
    expect(textarea().placeholder).toMatch(/send a message/i);
  });

  it("a structural read-only reason wins over the normal placeholder", () => {
    // readOnlyReason captures a session that can't take input at all, so it
    // must not be overridden by the default prompt.
    render(<Composer {...composerProps({ readOnlyReason: "Mirrored transcript" })} />);
    expect(textarea().placeholder).toBe("Mirrored transcript");
  });

  it("streaming shows the queued follow-up placeholder", () => {
    render(<Composer {...composerProps({ status: "streaming" })} />);
    expect(textarea().placeholder).toMatch(/send a follow-up/i);
  });

  it("resets the native text input when the Send button moves focus first", () => {
    const props = composerProps({ status: "streaming", isWorking: true });
    render(<Composer {...props} />);
    const ta = textarea();

    ta.focus();
    fireEvent.change(ta, { target: { value: "disabled" } });
    fireEvent.blur(ta);
    const focusSpy = vi.spyOn(ta, "focus");
    const blurSpy = vi.spyOn(ta, "blur");
    fireEvent.submit(ta.closest("form")!);

    expect(props.onSend).toHaveBeenCalledWith("disabled", undefined);
    expect(focusSpy).toHaveBeenCalledOnce();
    expect(blurSpy).toHaveBeenCalledOnce();
    expect(ta).toHaveValue("");
    expect(ta).not.toHaveFocus();
    expect(ta.placeholder).toMatch(/send a follow-up/i);
  });

  it("keeps the native input focused after a keyboard send", () => {
    const props = composerProps({ status: "streaming", isWorking: true });
    render(<Composer {...props} />);
    const ta = textarea();

    ta.focus();
    fireEvent.change(ta, { target: { value: "disabled" } });
    fireEvent.keyDown(ta, { key: "Enter" });

    expect(props.onSend).toHaveBeenCalledWith("disabled", undefined);
    expect(ta).toHaveValue("");
    expect(ta).toHaveFocus();
    expect(ta.placeholder).toMatch(/send a follow-up/i);
  });

  it("unreachable (host offline / local-stranded): composer is blocked", () => {
    // A message can't wake it, so the textarea is disabled and the banner
    // below is the only affordance.
    render(<Composer {...composerProps({ unreachable: true })} />);
    expect(textarea().disabled).toBe(true);
    expect(textarea().placeholder).toMatch(/reconnect below/i);
  });
});

// A pending elicitation parks the agent's turn server-side on the verdict
// Future — a message posted then just sits queued and unread until the card
// is answered. These tests pin the composer lock that surfaces that state.
describe("Composer pending elicitation", () => {
  /**
   * A real ElicitationBlock (no mocks) matching the shape the BlockStream
   * reducer emits for `response.elicitation_request` — the same blocks the
   * composer's pending-elicitation selector scans.
   */
  function elicitationBlock(overrides: Partial<ElicitationBlock> = {}): ElicitationBlock {
    return {
      type: "elicitation",
      ctx: { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null },
      elicitationId: "elic_1",
      targetSessionId: null,
      message: "Allow shell command?",
      phase: "tool_call",
      policyName: "ask-before-shell",
      contentPreview: "{}",
      requestedSchema: {},
      url: null,
      status: "pending",
      response: null,
      ...overrides,
    };
  }

  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    // The other describes in this file never set `blocks` — clear it so a
    // leftover pending elicitation can't lock their composers.
    useChatStore.setState({ blocks: [] });
    cleanup();
    vi.restoreAllMocks();
  });

  it("keeps the textarea typable but blocks sending while an elicitation is pending", () => {
    useChatStore.setState({ blocks: [elicitationBlock()] });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    // The textarea must stay ENABLED: disabling it ejects browser focus
    // mid-word when the prompt lands while the user is typing, and their
    // continued keystrokes silently vanish. Only sending is locked.
    expect(ta.disabled).toBe(false);
    expect(ta.placeholder).toBe("Respond to the pending request above to continue");

    // Typing keeps landing in the draft while the prompt is pending.
    fireEvent.change(ta, { target: { value: "typed while pending" } });
    expect(ta.value).toBe("typed while pending");

    // But Enter must not send — the submit() guard parks the draft until
    // the prompt is answered (a message sent now would sit queued unread).
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();

    // Send button stays off despite the draft — without the elicitation
    // gate, a non-empty draft would enable it.
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
  });

  it("keeps the interrupt button live while an elicitation is pending", () => {
    // Cancelling the turn is the other legitimate way out of a parked
    // elicitation — the lock must not take the stop control with it.
    // Fresh session id: the interrupt button only shows with no draft, and
    // the lock test above left a per-session draft behind for "conv_test".
    useChatStore.setState({ conversationId: "conv_interrupt", blocks: [elicitationBlock()] });
    render(<Composer {...composerProps({ isWorking: true, status: "streaming" })} />);
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeEnabled();
  });

  it("unlocks once the elicitation is responded", () => {
    useChatStore.setState({
      blocks: [elicitationBlock({ status: "responded", response: { action: "accept" } })],
    });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "carry on" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    // The verdict is in — the send path must be fully restored, not just
    // the visual disabled state.
    expect(onSend).toHaveBeenCalledWith("carry on", undefined);
  });

  it("ignores mirrored sub-agent elicitations addressed to a child session", () => {
    // A child's prompt mirrored into this chat doesn't park THIS session's
    // turn — inbox talk-back to the parent must keep working.
    useChatStore.setState({ blocks: [elicitationBlock({ targetSessionId: "conv_child" })] });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    const ta = textarea();

    expect(ta.disabled).toBe(false);
    fireEvent.change(ta, { target: { value: "status update please" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("status update please", undefined);
  });
});

describe("Composer reply quotes", () => {
  beforeEach(() => {
    clearSessionDrafts();
    localStorage.clear();
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      blocks: [],
      failedSendDraft: null,
      queuedMessages: [],
    });
  });

  afterEach(() => {
    cleanup();
    clearSessionDrafts();
    vi.restoreAllMocks();
  });

  it("appends reply quotes after the existing draft and sends them interleaved", () => {
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...props} ref={ref} />);

    fireEvent.change(textarea(), { target: { value: "My introduction" } });
    act(() => ref.current?.appendReplyQuote("First point"));
    expect(textarea()).toHaveValue("");
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue("My introduction");
    expect(
      screen.getByTestId("composer-reply-quote").querySelector("blockquote"),
    ).toHaveTextContent("First point");
    expect(screen.getByRole("button", { name: "Remove quote" })).toBeEnabled();

    fireEvent.change(textarea(), {
      target: { value: textarea().value + "My first answer" },
    });
    act(() => ref.current?.appendReplyQuote("Second point\nMore detail"));
    expect(textarea()).toHaveValue("");
    expect(screen.getByLabelText("Reply text before quote 2")).toHaveValue("My first answer");
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(2);
    expect(
      screen
        .getAllByRole("textbox")
        .every((input) => !(input as HTMLTextAreaElement).value.includes(">")),
    ).toBe(true);

    fireEvent.change(textarea(), {
      target: { value: textarea().value + "My second answer" },
    });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith(
      "My introduction\n\n> First point\n\nMy first answer\n\n> Second point\n> More detail\n\nMy second answer",
      undefined,
      {
        version: 1,
        quotes: [
          { before: "My introduction", text: "First point" },
          { before: "My first answer", text: "Second point\nMore detail" },
        ],
        text: "My second answer",
      },
    );
    expect(textarea()).toHaveValue("");
    expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(0);
  });

  it("focuses the textarea after the appended quote, even when the old caret was elsewhere", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "Existing draft" } });
    ta.setSelectionRange(0, 8);
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    act(() => ref.current?.appendReplyQuote("selected response text"));
    expect(ta).toHaveValue("");
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue("Existing draft");
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("selected response text");
    expect(document.activeElement).toBe(ta);
    expect(ta.selectionStart).toBe(ta.value.length);
    expect(ta.selectionEnd).toBe(ta.value.length);
  });

  it("appends on mobile without opening the software keyboard", () => {
    const matchMedia = window.matchMedia;
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      ...matchMedia(query),
      matches: query.includes("max-width"),
    }));
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    const ta = textarea();
    expect(document.activeElement).not.toBe(ta);

    act(() => ref.current?.appendReplyQuote("Selected on mobile"));
    expect(ta).toHaveValue("");
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Selected on mobile");
    expect(ta.selectionStart).toBe(ta.value.length);
    expect(document.activeElement).not.toBe(ta);
  });

  it.each([
    { disabled: true },
    { permissionLevel: 1 },
    { readOnlyReason: "Read-only session" },
    { unreachable: true },
  ])("does not insert into a disabled composer: %j", (overrides) => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps(overrides)} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("Not editable"));
    expect(textarea()).toHaveValue("");
    expect(hasSessionDraft("conv_test")).toBe(false);
  });

  it("removes a quote without stealing focus or reinserting it on rerender", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    const props = composerProps();
    const { rerender } = render(<Composer {...props} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("Original quote"));
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "Only my reply" } });
    ta.blur();
    fireEvent.click(screen.getByRole("button", { name: "Remove quote" }));

    rerender(<Composer {...props} ref={ref} status="streaming" />);
    expect(ta).toHaveValue("Only my reply");
    expect(document.activeElement).not.toBe(ta);
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith("Only my reply", undefined);
  });

  it("appends repeated selections once per click in StrictMode", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(
      <StrictMode>
        <Composer {...composerProps()} ref={ref} />
      </StrictMode>,
    );
    act(() => {
      ref.current?.appendReplyQuote("Same selection");
      ref.current?.appendReplyQuote("Same selection");
    });
    expect(textarea()).toHaveValue("");
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(2);
    expect(getSessionDraft("conv_test")?.text).toBe("> Same selection\n\n> Same selection");
  });

  it.each(["", "\n", "\n\n"])("reuses trailing line breaks (%j)", (trailing) => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    fireEvent.change(textarea(), { target: { value: `Draft${trailing}` } });
    act(() => ref.current?.appendReplyQuote("Quoted line\r\n\r\nAnother paragraph"));
    expect(getSessionDraft("conv_test")?.text).toBe(
      "Draft\n\n> Quoted line\n> \n> Another paragraph",
    );
    expect(screen.getByTestId("composer-reply-quote").querySelector("blockquote")).toHaveAttribute(
      "title",
      "Quoted line\n\nAnother paragraph",
    );
  });

  it("can send a quote-only draft and does not carry it into the next message", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    const props = composerProps({ isWorking: true, status: "streaming" });
    render(<Composer {...props} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("Quoted text"));
    expect(screen.getByRole("button", { name: "Send" })).toBeEnabled();
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenLastCalledWith("> Quoted text", undefined, {
      version: 1,
      quotes: [{ before: "", text: "Quoted text" }],
      text: "",
    });
    expect(textarea()).toHaveValue("");

    fireEvent.change(textarea(), { target: { value: "Next message" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenLastCalledWith("Next message", undefined);
  });

  it("restores interleaved quotes only in their original session", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("First quote"));
    fireEvent.change(textarea(), {
      target: { value: textarea().value + "My answer" },
    });
    act(() => ref.current?.appendReplyQuote("Second quote"));
    const draft = getSessionDraft("conv_test")?.text;

    act(() => useChatStore.setState({ conversationId: "conv_other" }));
    expect(textarea()).toHaveValue("");
    expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(0);
    act(() => ref.current?.appendReplyQuote("Other session's quote"));

    act(() => useChatStore.setState({ conversationId: "conv_test" }));
    expect(getSessionDraft("conv_test")?.text).toBe(draft);
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(2);
    expect(screen.getByLabelText("Reply text before quote 2")).toHaveValue("My answer");
    act(() => useChatStore.setState({ conversationId: "conv_other" }));
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Other session's quote");
  });

  it("keeps earlier replies editable, including replacing all their text", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    const props = composerProps();
    render(<Composer {...props} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("First quote"));
    fireEvent.change(textarea(), { target: { value: "Original answer" } });
    act(() => ref.current?.appendReplyQuote("Second quote"));
    const earlier = screen.getByLabelText("Reply text before quote 2");
    act(() => earlier.focus());
    fireEvent.change(earlier, { target: { value: "" } });
    expect(earlier).toBeInTheDocument();
    expect(earlier).toHaveFocus();
    fireEvent.change(earlier, { target: { value: "Rewritten answer" } });
    fireEvent.keyDown(earlier, { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith(
      "> First quote\n\nRewritten answer\n\n> Second quote",
      undefined,
      {
        version: 1,
        quotes: [
          { before: "", text: "First quote" },
          { before: "Rewritten answer", text: "Second quote" },
        ],
        text: "",
      },
    );
    expect(textarea()).toHaveFocus();
  });

  it("appends after the entire draft when an earlier reply is focused", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("First quote"));
    fireEvent.change(textarea(), { target: { value: "First answer" } });
    act(() => ref.current?.appendReplyQuote("Second quote"));
    fireEvent.change(textarea(), { target: { value: "Second answer" } });
    act(() => screen.getByLabelText("Reply text before quote 2").focus());
    act(() => ref.current?.appendReplyQuote("Third quote"));
    expect(screen.getByLabelText("Reply text before quote 2")).toHaveValue("First answer");
    expect(screen.getByLabelText("Reply text before quote 3")).toHaveValue("Second answer");
    expect(textarea()).toHaveFocus();
    expect(textarea()).toHaveValue("");
  });

  it("removes middle and final quote cards without deleting surrounding replies", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    const props = composerProps();
    render(<Composer {...props} ref={ref} />);
    fireEvent.change(textarea(), { target: { value: "Introduction" } });
    act(() => ref.current?.appendReplyQuote("First quote"));
    fireEvent.change(textarea(), { target: { value: "First answer" } });
    act(() => ref.current?.appendReplyQuote("Second quote"));
    fireEvent.change(textarea(), { target: { value: "Second answer" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Remove quote" })[0]!);
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue(
      "Introduction\n\nFirst answer",
    );
    fireEvent.click(screen.getByRole("button", { name: "Remove quote" }));
    expect(textarea()).toHaveValue("Introduction\n\nFirst answer\n\nSecond answer");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith(
      "Introduction\n\nFirst answer\n\nSecond answer",
      undefined,
    );
  });

  it.each(["intro\n> quote\nreply", "> quoted\ncontinued", "\nNotes:\n\n> Example text\n\n"])(
    "restores unannotated Markdown as editable text: %j",
    (text) => {
      setSessionDraft("conv_test", { text, files: [] });
      const props = composerProps();
      render(<Composer {...props} />);
      expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(0);
      expect(textarea()).toHaveValue(text);
      fireEvent.keyDown(textarea(), { key: "Enter" });
      expect(props.onSend).toHaveBeenCalledWith(text.trim(), undefined);
    },
  );

  it("restores only actual cards beside authored quotes and an unfinished code fence", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);
    const before = "Notes:\n> authored\ncontinued\n\n\n";
    const tail = "~~~markdown\n> code example\n";
    fireEvent.change(textarea(), { target: { value: before } });
    act(() => ref.current?.appendReplyQuote("Actual Reply quote"));
    fireEvent.change(textarea(), { target: { value: tail } });
    act(() => ref.current?.appendReplyQuote("Quote after unfinished fence"));
    const saved = getSessionDraft("conv_test");

    act(() => useChatStore.setState({ conversationId: "other" }));
    act(() => useChatStore.setState({ conversationId: "conv_test" }));
    expect(getSessionDraft("conv_test")).toEqual(saved);
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(2);
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue(before);
    expect(screen.getByLabelText("Reply text before quote 2")).toHaveValue(tail);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove quote" })[1]!);
    fireEvent.click(screen.getByRole("button", { name: "Remove quote" }));
    expect(textarea()).toHaveValue(before + tail);
  });

  it("recalls actual cards from history without reclassifying authored Markdown", () => {
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    const { unmount } = render(<Composer {...props} ref={ref} />);
    const before = "Intro\n> authored\nlazy continuation\n\n";
    fireEvent.change(textarea(), { target: { value: before } });
    act(() => ref.current?.appendReplyQuote("Actual card"));
    fireEvent.change(textarea(), { target: { value: "My answer\n\n\n" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    const sent = vi.mocked(props.onSend).mock.calls[0]!;
    unmount();

    render(<Composer {...props} />);
    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    expect(screen.getAllByTestId("composer-reply-quote")).toHaveLength(1);
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue(before);
    expect(textarea()).toHaveValue("My answer\n\n\n");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenLastCalledWith(...sent);
  });

  it("exits history recall when a quote card is removed", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "Introduction", text: "Actual card" }],
      text: "Answer to keep",
    };
    localStorage.setItem(
      "omnigent:prompt-history:conv_test",
      JSON.stringify([{ text: serializeReplyDraft(replyDraft), replyDraft }]),
    );
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "Draft from before recall" } });
    textarea().setSelectionRange(0, 0);
    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    fireEvent.click(screen.getByRole("button", { name: "Remove quote" }));
    const edited = "Introduction\n\nAnswer to keep";
    expect(textarea()).toHaveValue(edited);
    textarea().setSelectionRange(edited.length, edited.length);
    fireEvent.keyDown(textarea(), { key: "ArrowDown" });
    expect(textarea()).toHaveValue(edited);
    expect(getSessionDraft("conv_test")?.text).toBe(edited);
  });

  it("exits history recall on the first manual text edit", () => {
    localStorage.setItem("omnigent:prompt-history:conv_test", JSON.stringify(["Older prompt"]));
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "Draft from before recall" } });
    textarea().setSelectionRange(0, 0);
    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    expect(textarea()).toHaveValue("Older prompt");
    const edited = "My edited prompt";
    fireEvent.change(textarea(), { target: { value: edited } });
    textarea().setSelectionRange(edited.length, edited.length);
    fireEvent.keyDown(textarea(), { key: "ArrowDown" });
    expect(textarea()).toHaveValue(edited);
  });

  it.each([false, true])("restores failed sends using explicit metadata only: %s", (structured) => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "intro\n> authored\ncontinued", text: "Actual card" }],
      text: "Answer",
    };
    const text = structured ? serializeReplyDraft(replyDraft) : "intro\n> authored\ncontinued";
    const props = composerProps();
    render(<Composer {...props} />);
    act(() =>
      useChatStore.setState({
        failedSendDraft: {
          conversationId: "conv_test",
          text,
          files: [],
          ...(structured ? { replyDraft } : {}),
        },
      }),
    );
    expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(structured ? 1 : 0);
    expect(textarea()).toHaveValue(structured ? "Answer" : text);
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(vi.mocked(props.onSend).mock.calls[0]?.[0]).toBe(text);
    if (structured) expect(vi.mocked(props.onSend).mock.calls[0]?.[2]).toEqual(replyDraft);
  });

  it.each([false, true])(
    "edits and persists queued messages with explicit metadata only: %s",
    (structured) => {
      const replyDraft: StoredReplyDraft = {
        version: 1,
        quotes: [{ before: "intro\n> authored\ncontinued", text: "Actual card" }],
        text: "Answer",
      };
      const text = structured ? serializeReplyDraft(replyDraft) : "intro\n> authored\ncontinued";
      useChatStore.setState({
        status: "streaming",
        sessionStatus: "running",
        queuedMessages: [
          {
            queueId: "q_reply",
            conversationId: "conv_test",
            text,
            ...(structured ? { replyDraft } : {}),
          },
        ],
      });
      const props = composerProps({ status: "streaming", isWorking: true });
      renderWithTooltips(<Composer {...props} />);
      fireEvent.click(screen.getByRole("button", { name: "Edit queued message" }));
      expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(structured ? 1 : 0);
      expect(textarea()).toHaveValue(structured ? "Answer" : text);
      expect(useChatStore.getState().queuedMessages).toHaveLength(0);
      expect(getSessionDraft("conv_test")?.text).toBe(text);
      expect(getSessionDraft("conv_test")?.replyDraft).toEqual(structured ? replyDraft : undefined);
      fireEvent.keyDown(textarea(), { key: "Enter" });
      expect(vi.mocked(props.onSend).mock.calls[0]?.[0]).toBe(text);
      if (structured) expect(vi.mocked(props.onSend).mock.calls[0]?.[2]).toEqual(replyDraft);
    },
  );

  it("keeps mention markers in the structured payload used to restore a failed send", () => {
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    useChatStore.setState({ sessionHarness: "codex-native" });
    render(<Composer {...props} ref={ref} />);
    act(() =>
      useChatStore.setState({
        pendingComposerAttachments: [{ path: "src/example.ts", isDir: false }],
      }),
    );
    act(() => ref.current?.appendReplyQuote("Actual card"));
    fireEvent.change(textarea(), { target: { value: "My answer" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    const [text, , replyDraft] = vi.mocked(props.onSend).mock.calls[0]!;
    expect(text).toBe("[Attached file: src/example.ts]\n\n> Actual card\n\nMy answer");
    expect(serializeReplyDraft(replyDraft!)).toBe(text);
    act(() =>
      useChatStore.setState({
        failedSendDraft: { conversationId: "conv_test", text, files: [], replyDraft },
      }),
    );
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Actual card");
    expect(screen.getByLabelText("Reply text before quote 1")).toHaveValue(
      "[Attached file: src/example.ts]\n\n",
    );
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(vi.mocked(props.onSend).mock.calls[1]?.[0]).toBe(text);
  });

  it("sends slash-command-looking replies as part of the quoted message", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    const props = composerProps();
    render(<Composer {...props} ref={ref} />);
    act(() => ref.current?.appendReplyQuote("Explain /help"));
    fireEvent.change(textarea(), { target: { value: "/help" } });
    expect(activeRow()).toBeNull();
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith("> Explain /help\n\n/help", undefined, {
      version: 1,
      quotes: [{ before: "", text: "Explain /help" }],
      text: "/help",
    });
  });
});

// Attaching a file via the paperclip button routes through the hidden file
// <input>, whose click (and the OS file dialog) pulls focus off the composer.
// The change handler must hand focus back so the user can keep typing the
// message that goes with the attachment — without this the caret is lost and
// the next keystroke does nothing until the chat box is clicked again.
describe("Composer file-attachment focus", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
    // Drafts persist per conversation: without this, a file attached by one
    // test is restored into the next one's composer.
    clearSessionDrafts();
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /** The hidden attachment file <input> (the paperclip button proxies to it). */
  function fileInput(): HTMLInputElement {
    const el = document.querySelector('input[type="file"]') as HTMLInputElement | null;
    if (!el) throw new Error("file input not found");
    return el;
  }

  it("focuses the textarea after a file is attached", () => {
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    // The mount effect focuses on conversation bind; blur so the assertion
    // proves the attach handler re-focused, not the leftover mount focus.
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });
    fireEvent.change(fileInput(), { target: { files: [file] } });

    expect(document.activeElement).toBe(ta);
  });

  it("marks the textarea with data-has-draft for an attachment-only draft", () => {
    // The approve hotkey's drafting guard only sees the focused element, so
    // the composer must advertise non-text drafts (attachments, mentions) on
    // the textarea itself — with an empty value, an attached file is still a
    // sendable draft, and Cmd/Ctrl+Enter must read as send intent there.
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    expect(ta.getAttribute("data-has-draft")).toBeNull();

    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });
    fireEvent.change(fileInput(), { target: { files: [file] } });

    expect(ta.value).toBe("");
    expect(ta.getAttribute("data-has-draft")).toBe("true");
  });

  it("does not focus the textarea when the attachment is rejected", () => {
    // An unsupported type is dropped by validateAttachments, so no file is
    // added — and with nothing attached there's no reason to yank focus back.
    render(<Composer {...composerProps()} />);
    const ta = textarea();
    ta.blur();
    expect(document.activeElement).not.toBe(ta);

    const bad = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(fileInput(), { target: { files: [bad] } });

    expect(document.activeElement).not.toBe(ta);
  });

  // The drop target is the chat column (``[data-chat-surface]``, SessionLayout),
  // which the composer resolves from its own card. Unhandled, a drop on the
  // transcript makes the browser navigate away to render the file.
  it("attaches a file dropped elsewhere in the chat column", () => {
    render(
      <div data-chat-surface>
        <div data-testid="transcript">transcript</div>
        <Composer {...composerProps()} />
      </div>,
    );
    const transcript = screen.getByTestId("transcript");
    // ``types`` is the only file signal available mid-drag.
    fireEvent.dragEnter(transcript, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.getByTestId("file-drop-overlay")).toBeTruthy();

    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });
    fireEvent.drop(transcript, { dataTransfer: { types: ["Files"], files: [file] } });

    // getAllBy: the chip pairs the visible name with a hover title.
    expect(screen.getAllByText("shot.png").length).toBeGreaterThan(0);
    expect(screen.queryByTestId("file-drop-overlay")).toBeNull();
  });

  // Outside the column — sidebar, workspace rail — a file drag is not an
  // attachment.
  it("ignores a file dropped outside the chat column", () => {
    render(
      <div>
        <div data-chat-surface>
          <Composer {...composerProps()} />
        </div>
        <div data-testid="sidebar">sidebar</div>
      </div>,
    );
    const sidebar = screen.getByTestId("sidebar");
    const file = new File([new Uint8Array(10)], "shot.png", { type: "image/png" });

    fireEvent.dragEnter(sidebar, { dataTransfer: { types: ["Files"], files: [] } });
    expect(screen.queryByTestId("file-drop-overlay")).toBeNull();
    fireEvent.drop(sidebar, { dataTransfer: { types: ["Files"], files: [file] } });

    expect(screen.queryByText("shot.png")).toBeNull();
  });

  it("clears the rejection notice once the user types", () => {
    // The rejected file is never attached, so there is no chip to remove and
    // nothing else clears the notice. Left sticky it reads as a blocker on a
    // composer that can actually be submitted.
    render(<Composer {...composerProps()} />);
    const bad = new File([new Uint8Array(10)], "clip.mp4", { type: "video/mp4" });
    fireEvent.change(fileInput(), { target: { files: [bad] } });
    expect(screen.getByText(/can't be attached/)).toBeTruthy();

    fireEvent.change(textarea(), { target: { value: "never mind, just a question" } });

    expect(screen.queryByText(/can't be attached/)).toBeNull();
  });
});

// The "Chatting with sub-agent …" tray peeks above the composer only when a
// sub-agent label is passed (the active session is a child). It must name the
// sub-agent so the composer reads as messaging the child, not the orchestrator.
describe("Composer sub-agent tray", () => {
  beforeEach(() => {
    useChatStore.setState({ conversationId: "conv_test", skills: [] });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  /** The sub-agent tray element, or null when not rendered. */
  function tray(): Element | null {
    return document.querySelector('[data-testid="composer-subagent-tray"]');
  }

  it("does not render the tray for a top-level session (no label)", () => {
    render(<Composer {...composerProps()} />);
    expect(tray()).toBeNull();
  });

  it("does not render the tray for an empty label", () => {
    // null is the top-level default; an empty string must also not peek a
    // nameless tray.
    render(<Composer {...composerProps({ subAgentLabel: "" })} />);
    expect(tray()).toBeNull();
  });

  it("renders the sub-agent name when a label is passed", () => {
    render(<Composer {...composerProps({ subAgentLabel: "check-account-eligibility" })} />);
    expect(tray()).not.toBeNull();
    // The name proves the passed label reaches the rendered tray, not just
    // that some tray exists.
    expect(screen.getByText("check-account-eligibility")).toBeTruthy();
    expect(screen.getByText(/Chatting with sub-agent/)).toBeTruthy();
  });
});

describe("Composer — queued-message flush gating", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ queuedMessages: [] });
  });

  // Regression (Polly review 3a): the level-triggered flush effect must NOT
  // drain the queue while the session is unreachable — flushing would POST
  // into a void, bypassing onSend's reconnect dialog. It must drain once
  // reachable again.
  it("holds the queue while unreachable, then flushes when reachable", async () => {
    const sendSpy = vi.fn().mockResolvedValue(undefined);
    useChatStore.setState({
      conversationId: "conv_test",
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "idle",
      send: sendSpy,
      queuedMessages: [{ queueId: "q_1", text: "held", conversationId: "conv_test" }],
    });

    // Idle + a waiting head, but unreachable → the effect must not flush.
    // Wrapped in TooltipProvider since the queued-message strip renders the
    // steer button's tooltip.
    const { rerender } = renderWithTooltips(<Composer {...composerProps({ unreachable: true })} />);
    await waitFor(() => expect(sendSpy).not.toHaveBeenCalled());
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);

    // Becomes reachable → the effect re-fires and drains the head.
    rerender(
      <TooltipProvider>
        <Composer {...composerProps({ unreachable: false })} />
      </TooltipProvider>,
    );
    await waitFor(() => expect(sendSpy).toHaveBeenCalledTimes(1));
    expect(sendSpy.mock.calls[0]!.slice(0, 2)).toEqual(["held", "agent_xyz"]);
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
  });
});

describe("Composer config gear", () => {
  beforeEach(() => {
    useChatStore.setState({
      conversationId: "conv_test",
      skills: [],
      selectedModel: null,
      sessionModelOverride: null,
      llmModel: null,
      nativeVendorOwnsModel: false,
      selectedEffort: null,
      costControlModeOverride: null,
      // Opening the gear re-reads the routing switches; stub the fetch away.
      refreshSessionOverrides: vi.fn().mockResolvedValue(undefined),
    });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  const gear = () => document.querySelector('[data-testid="composer-config-gear"]');

  it("renders when the session has a switchable knob (effort)", () => {
    renderWithTooltips(<Composer {...composerProps({ showEffort: true })} />);
    expect(gear()).not.toBeNull();
  });

  it("keeps the shared trigger disabled when there is nothing to configure", () => {
    // No models, no effort, not routable → nothing to configure.
    renderWithTooltips(
      <Composer
        {...composerProps({ showEffort: false, showModels: false, costRoutingEligible: false })}
      />,
    );
    expect(gear()).toBeDisabled();
  });

  it("soft-disables the gear on a read-only session (aria-disabled, click no-ops)", () => {
    renderWithTooltips(<Composer {...composerProps({ showEffort: true, permissionLevel: 1 })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "true");
    // Soft-disable, not native `disabled`: the click is guarded but the button
    // stays hover-able so its config tooltip still shows.
    expect(gear()).toHaveProperty("disabled", false);
    openSessionConfig();
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("soft-disables the gear when the session is unreachable (host offline / stranded)", () => {
    // No message can wake an unreachable session, so a config change has
    // nothing to apply to — the gear is inert like the composer.
    renderWithTooltips(<Composer {...composerProps({ showEffort: true, unreachable: true })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "true");
    openSessionConfig();
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("keeps the gear live on an asleep session (change persists and applies on wake)", () => {
    // Asleep/starting/unknown sessions accept sends (which wake the runner),
    // and a config PATCH persists server-side and applies on the next
    // wake/turn — so the gear stays live wherever the composer does.
    renderWithTooltips(<Composer {...composerProps({ showEffort: true })} />);
    expect(gear()).toHaveAttribute("aria-disabled", "false");
    openSessionConfig();
    expect(screen.queryByTestId("composer-agent-menu")).not.toBeNull();
  });

  it("still shows the config tooltip on a disabled gear (soft-disable preserves hover)", async () => {
    useChatStore.setState({ selectedEffort: "high", sessionHarness: "claude-sdk" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: true,
          showModels: true,
          modelPickerKind: "claude",
          unreachable: true,
        })}
      />,
    );
    // Even soft-disabled, the read-only summary must remain visible on hover.
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model:");
  });

  it("shows a hover summary with Harness/Model/Effort rows and no Permissions row", async () => {
    useChatStore.setState({ selectedEffort: "high", sessionHarness: "claude-sdk" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: true,
          showModels: true,
          modelPickerKind: "claude",
          selectedAgentId: "a1",
          agents: [{ id: "a1", name: "claude-native-ui" } as never],
        })}
      />,
    );
    // Radix tooltips open on focus; focusing the trigger reveals the content.
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Harness:");
    expect(tip.textContent).toContain("Model:");
    expect(tip.textContent).toContain("Effort:");
    // Effort is switchable in-session; permission mode is not, so it must be absent.
    expect(tip.textContent).not.toContain("Permission mode");
  });

  it("reflects Smart Routing in the Model row of the summary when routing is on", async () => {
    useChatStore.setState({ costControlModeOverride: "on" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model: Smart Routing");
  });

  it("omits the Effort row from the summary when routing is on (router owns effort)", async () => {
    useChatStore.setState({ costControlModeOverride: "on", selectedEffort: "high" });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    fireEvent.focus(gear()!);
    const tip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tip.textContent).toContain("Model: Smart Routing");
    // The router picks effort per turn, so a pinned effort must not show.
    expect(tip.textContent).not.toContain("Effort:");
  });

  it("omits Advanced settings and its trailing separator from the picker", () => {
    renderWithTooltips(
      <Composer
        {...composerProps({ showEffort: true, showModels: true, modelPickerKind: "claude" })}
      />,
    );
    openSessionConfig();
    expect(screen.queryByTestId("composer-advanced-settings")).toBeNull();
    expect(screen.queryByText("Advanced settings…")).toBeNull();
    const menu = screen.getByTestId("composer-agent-menu");
    expect(menu.lastElementChild).toBe(screen.getByTestId("composer-agent-edit"));
  });

  it("opens mobile model and effort settings in place with Back navigation", async () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = ((query: string) => ({
      ...originalMatchMedia(query),
      matches: query.includes("max-width"),
    })) as typeof window.matchMedia;
    try {
      renderWithTooltips(
        <Composer
          {...composerProps({
            showModels: true,
            showEffort: true,
            modelPickerKind: "claude",
            codexModelOptions: CLAUDE_MODEL_OPTIONS,
          })}
        />,
      );
      fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
      fireEvent.click(screen.getByTestId("composer-agent-edit"));
      const menu = await screen.findByTestId("composer-agent-menu");
      expect(within(menu).getByTestId("composer-agent-efforts")).toBeTruthy();
      expect(screen.getAllByRole("menu")).toHaveLength(1);
      fireEvent.click(screen.getByTestId("composer-agent-config-back"));
      expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
      expect(screen.getByTestId("composer-agent-edit")).toBeTruthy();
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("offers inline effort choices without an Advanced entry in the session picker", async () => {
    useChatStore.setState({
      selectedEffort: "xhigh",
      sessionHarness: "claude-native",
      llmModel: "opus",
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: true,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: CLAUDE_MODEL_OPTIONS,
          effortLevels: ["low", "medium", "high", "xhigh", "max"],
        })}
      />,
    );
    fireEvent.keyDown(screen.getByTestId("composer-config-gear"), { key: "ArrowDown" });
    fireEvent.keyDown(screen.getByTestId("composer-agent-edit"), { key: "ArrowRight" });
    expect(await screen.findByTestId("composer-agent-effort-xhigh")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByTestId("composer-agent-effort-xhigh")).toHaveTextContent("xHigh");
    expect(screen.queryByText("Advanced settings…")).toBeNull();
  });

  it("uses the Default sentinel when Kiro marks no catalog row as default", async () => {
    const options = [
      { id: "auto", displayName: "Automatic", isDefault: false },
      { id: "provider-latest", displayName: "Latest", isDefault: false },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "kiro",
          codexModelOptions: options,
        })}
      />,
    );

    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    expect(screen.getByTestId("composer-agent-models")).toHaveTextContent("Default");
  });

  it("names the model Codex's Default resolves to, like the new-session gear", async () => {
    const options = [
      { id: "gpt-5.6-sol", displayName: "GPT-5.6-Sol" },
      { id: "gpt-5.6-luna", displayName: "GPT-5.6-Luna", isDefault: true },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "codex",
          codexModelOptions: options,
        })}
      />,
    );

    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    // A bare "Default" was the bug: this gear and the new-session gear named
    // the same unpinned session's model differently, so neither told the user
    // which model Codex would actually run.
    expect(screen.getByRole("menuitemcheckbox", { name: "GPT-5.6-Luna" })).toBeTruthy();
  });

  it("names the model Claude's Default resolves to, like Codex", async () => {
    // The claude branch used to discard the catalog's isDefault marker, so
    // its gear read a bare "Default" while codex named the model — the same
    // shared labeling now serves both harnesses.
    const options = [
      { id: "sonnet", model: "claude-sonnet-5", displayName: "Sonnet 5" },
      {
        id: "opus[1m]",
        model: "claude-opus-4-8[1m]",
        displayName: "Opus 4.8 (1M context)",
        isDefault: true,
      },
    ];
    renderWithTooltips(
      <Composer
        {...composerProps({
          showEffort: false,
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    expect(screen.getByRole("menuitemcheckbox", { name: "Opus 4.8 (1M context)" })).toBeTruthy();
  });

  it("does not open the modal via bare /model when the gear is disabled (unreachable)", async () => {
    // Bare /model bumps the open nonce; on an unreachable session the gear is
    // inert, so the nonce must NOT open a modal that can't apply a change.
    const options = [{ id: "opus", model: "opus", displayName: "Opus" }] as never;
    useChatStore.setState({ codexModelOptions: options });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          unreachable: true,
          codexModelOptions: options,
        })}
      />,
    );
    const modelTextarea = document.querySelector("textarea") as HTMLTextAreaElement;
    fireEvent.change(modelTextarea, { target: { value: "/model" } });
    fireEvent.keyDown(modelTextarea, { key: "Enter", code: "Enter" });
    // Give the nonce effect a tick; the modal must stay closed.
    await waitFor(() => expect(gear()).toHaveAttribute("aria-disabled", "true"));
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("keeps Codex models in the primary picker alongside Smart Routing", async () => {
    // Regression: Codex has a Model dropdown, so Smart Routing must be an option
    // inside it (like Claude) — NOT a separate switch alongside the dropdown.
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "codex",
          costRoutingEligible: true,
        })}
      />,
    );
    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    expect(screen.getByTestId("composer-agent-models")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
  });

  it("serializes immediate model and effort changes", async () => {
    // Claude-native types /model and /effort as separate terminal commands, so
    // Save must await the model PATCH before firing effort — otherwise the two
    // injections interleave into one bad line. This pins that ordering.
    const calls: string[] = [];
    let resolveModel: () => void = () => {};
    const setModel = vi.fn().mockImplementation(() => {
      calls.push("model");
      return new Promise<void>((r) => {
        resolveModel = r;
      });
    });
    const setEffort = vi.fn().mockImplementation(() => {
      calls.push("effort");
      return Promise.resolve();
    });
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({
      setModel,
      setEffort,
      selectedEffort: "high",
      codexModelOptions: options,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );
    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    // Draft a new model and a new effort.
    fireEvent.click(
      document.querySelector('[data-testid="composer-agent-model-sonnet"]') as Element,
    );
    fireEvent.click(document.querySelector('[data-testid="composer-agent-effort-low"]') as Element);
    // Draft only — no live commit yet.
    expect(setModel).toHaveBeenCalledTimes(1);
    expect(setEffort).not.toHaveBeenCalled();

    // Model fires first and effort waits for its promise to resolve.
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
    );
    expect(setEffort).not.toHaveBeenCalled();
    resolveModel();
    await waitFor(() =>
      expect(screen.getByTestId("composer-agent-effort-low")).not.toHaveAttribute("data-disabled"),
    );
    fireEvent.click(screen.getByTestId("composer-agent-effort-low"));
    await waitFor(() => expect(setEffort).toHaveBeenCalledWith("low"));
    expect(calls).toEqual(["model", "effort"]);
  });

  it("recomputes the Codex effort ladder after a confirmed model change and drops an unsupported level", async () => {
    // Codex advertises a per-model effort ladder. Drafting a lower-ceiling
    // model (Luna, no "ultra") must refresh the dropdown to that model's levels
    // and drop a picked level it can't run — else Save would send Sol's "ultra"
    // to Luna, and the dropdown would show a rung Luna rejects.
    const codexOptions = [
      {
        id: "gpt-5.6-sol",
        model: "gpt-5.6-sol",
        displayName: "GPT-5.6-Sol",
        isDefault: true,
        supportedReasoningEfforts: [
          { reasoningEffort: "low" },
          { reasoningEffort: "medium" },
          { reasoningEffort: "high" },
          { reasoningEffort: "xhigh" },
          { reasoningEffort: "max" },
          { reasoningEffort: "ultra" },
        ],
      },
      {
        id: "gpt-5.6-luna",
        model: "gpt-5.6-luna",
        displayName: "GPT-5.6-Luna",
        supportedReasoningEfforts: [
          { reasoningEffort: "low" },
          { reasoningEffort: "medium" },
          { reasoningEffort: "high" },
          { reasoningEffort: "xhigh" },
          { reasoningEffort: "max" },
        ],
      },
    ] as never;
    useChatStore.setState({
      setModel: vi.fn().mockResolvedValue(undefined),
      setEffort: vi.fn().mockResolvedValue(undefined),
      selectedEffort: "ultra",
      llmModel: "gpt-5.6-sol",
      codexModelOptions: codexOptions,
      refreshSessionOverrides: vi.fn().mockResolvedValue(undefined),
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          showEffort: true,
          modelPickerKind: "codex",
          effortLevels: ["low", "medium", "high", "xhigh", "max", "ultra"],
          codexModelOptions: codexOptions,
        })}
      />,
    );
    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");

    // Sol starts on ultra.
    expect(screen.getByTestId("composer-agent-effort-ultra")).toHaveAttribute(
      "aria-checked",
      "true",
    );

    // Draft a switch to Luna, whose ceiling is "max".
    fireEvent.click(
      document.querySelector('[data-testid="composer-agent-model-gpt-5.6-luna"]') as Element,
    );

    // The picked ultra is dropped (back to Default) and no longer offered,
    // while Luna's own max stays.
    await waitFor(() => expect(useChatStore.getState().setEffort).toHaveBeenCalledWith(null));
    act(() => useChatStore.setState({ llmModel: "gpt-5.6-luna", selectedEffort: null }));
    expect(screen.queryByTestId("composer-agent-effort-default")).toBeNull();
    expect(document.querySelector('[data-testid="composer-agent-effort-ultra"]')).toBeNull();
    expect(document.querySelector('[data-testid="composer-agent-effort-max"]')).not.toBeNull();
  });

  it("re-pins the model when turning Smart Routing off, even if the shown model is unchanged", async () => {
    // Routing-on clears the applied override but keeps the cross-session sticky
    // (selectedModel), so the modal shows that model as "resolved". Turning
    // routing off by re-picking that same model must still PATCH setModel —
    // otherwise the pin is silently dropped and the session falls back to
    // default. Regression for the resolvedModelId short-circuit false-negative.
    const setModel = vi.fn().mockResolvedValue(undefined);
    const setCostControlMode = vi.fn().mockResolvedValue(undefined);
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({
      setModel,
      setCostControlMode,
      // Routing on + leftover sticky "opus", no applied override.
      costControlModeOverride: "on",
      selectedModel: "opus",
      sessionModelOverride: null,
      codexModelOptions: options,
    });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
          codexModelOptions: options,
        })}
      />,
    );
    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    // Turn routing off by picking the same model the modal already shows.
    fireEvent.click(document.querySelector('[data-testid="composer-agent-model-opus"]') as Element);
    // The pin must be re-applied AND routing cleared.
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("opus", { expectConfirmation: true }),
    );
    expect(setCostControlMode).toHaveBeenCalledWith("off");
  });

  it("keeps Claude models in the primary picker alongside Smart Routing", async () => {
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          costRoutingEligible: true,
        })}
      />,
    );
    await openSessionModels();
    await screen.findByTestId("composer-agent-config-menu");
    // Claude gets the Model select instead of a standalone routing switch.
    expect(screen.getByTestId("composer-agent-models")).toBeTruthy();
    expect(screen.queryByTestId("composer-config-smart-routing")).toBeNull();
  });

  // A routed session is pinned to the router's fully-qualified pick, which the
  // harness catalog carries only under an alias — the Model row used to render
  // blank because no option declared that value.
  describe("routed model not in the harness catalog", () => {
    const ROUTED = "databricks-claude-opus-4-8";
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;

    async function openModalOnRoutedSession(setModel = vi.fn().mockResolvedValue(undefined)) {
      useChatStore.setState({
        setModel,
        codexModelOptions: options,
        sessionModelOverride: ROUTED,
        // The forwarder reported the routed model — the display authority.
        llmModel: ROUTED,
        // Routing pinned the model; the session's own routing switch is unset.
        costControlModeOverride: null,
      });
      renderWithTooltips(
        <Composer
          {...composerProps({
            showModels: true,
            modelPickerKind: "claude",
            costRoutingEligible: true,
            codexModelOptions: options,
          })}
        />,
      );
      await openSessionModels();
      await screen.findByTestId("composer-agent-config-menu");
      return setModel;
    }

    it("names the model the session is on instead of rendering blank", async () => {
      await openModalOnRoutedSession();
      expect(screen.getByTestId("composer-agent-models")).toHaveTextContent("claude-opus-4-8");
    });

    it("pins nothing when opening and closing the model picker", async () => {
      const setModel = await openModalOnRoutedSession();
      fireEvent.keyDown(screen.getByTestId("composer-agent-config-menu"), { key: "Escape" });
      expect(setModel).not.toHaveBeenCalled();
    });

    it("still commits a real pick made from the catalog", async () => {
      const setModel = await openModalOnRoutedSession();
      fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Sonnet" }));
      await waitFor(() =>
        expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
      );
    });
  });
});

describe("shouldQueueSend", () => {
  const q = (conversationId: string): QueuedMessage => ({
    queueId: `q_${conversationId}`,
    text: "queued",
    conversationId,
  });

  it("sends directly (no queue) for a brand-new chat with no conversation", () => {
    expect(shouldQueueSend(null, "streaming", "running", [])).toBe(false);
  });

  it("queues while the session is busy (streaming or running)", () => {
    expect(shouldQueueSend("conv_a", "streaming", "idle", [])).toBe(true);
    expect(shouldQueueSend("conv_a", "idle", "running", [])).toBe(true);
  });

  it("sends directly when idle and nothing is queued for this conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [])).toBe(false);
  });

  it("sends directly on `waiting` (turn ended, only background work remains)", () => {
    // A background shell / still-running sub-agent keeps the session in
    // `waiting`, but the server's turn gate is already free — a new message
    // must start a fresh turn rather than stalling in the client queue.
    expect(shouldQueueSend("conv_a", "idle", "waiting", [])).toBe(false);
  });

  it("queues when idle but this conversation already has a queued message", () => {
    // The ordering fix: an idle flicker must not let a later send overtake the
    // still-queued earlier one.
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_a")])).toBe(true);
  });

  it("ignores queued messages belonging to a different conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_b")])).toBe(false);
  });

  it("sends directly while busy when alwaysSteer is on", () => {
    // The whole point of the preference: a mid-turn follow-up is POSTed now
    // (steered) instead of parking in the queue strip.
    expect(shouldQueueSend("conv_a", "streaming", "idle", [], true)).toBe(false);
    expect(shouldQueueSend("conv_a", "idle", "running", [], true)).toBe(false);
  });

  it("still queues under alwaysSteer when this conversation has a queued message", () => {
    // The ordering guard outranks always-steer: draining must stay in order, so
    // a direct send can't overtake a still-queued earlier one.
    expect(shouldQueueSend("conv_a", "streaming", "running", [q("conv_a")], true)).toBe(true);
  });
});
