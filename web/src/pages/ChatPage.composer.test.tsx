import { create } from "zustand";
import type { SkillSummary, SkillsStatus } from "@/lib/types";

import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as GoalApiModule from "@/lib/goalApi";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type { ChildSessionInfo } from "@/hooks/useChildSessions";
import type * as FileViewerContextModule from "@/shell/FileViewerContext";

import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createRef, StrictMode, type ComponentRef, type ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore, type ChatState, type QueuedMessage } from "@/store/chatStore";
import {
  clearSessionDrafts,
  getSessionDraft,
  hasSessionDraft,
  setSessionDraft,
} from "@/lib/sessionDrafts";
import { setOmnigentHostConfig } from "@/lib/host";
import * as host from "@/lib/host";
import * as identity from "@/lib/identity";
import {
  getSessionModelLabelCacheKey,
  readSessionModelLabelCache,
} from "@/lib/sessionModelLabelCache";
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
const { composerGitStatusArgsSpy, composerGitStatusSnapshot } = vi.hoisted(() => ({
  composerGitStatusArgsSpy: vi.fn(),
  composerGitStatusSnapshot: {
    branch: null as string | null,
    branchState: "unknown" as "loading" | "branch" | "detached" | "not-git" | "unknown",
    isWorktree: null as boolean | null,
    worktreePath: null as string | null,
    creationBranch: null as string | null,
    repoNameWithOwner: "omnigent-ai/omnigent" as string | null,
    githubState: "ready" as "loading" | "ready" | "unknown",
    prCount: 0,
    prNumber: null as number | null,
    refresh: vi.fn(),
    refreshing: false,
  },
}));
const { openGithubTabMock } = vi.hoisted(() => ({ openGithubTabMock: vi.fn() }));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: (args: unknown) => {
    composerGitStatusArgsSpy(args);
    return composerGitStatusSnapshot;
  },
}));
vi.mock("@/shell/FileViewerContext", async (importOriginal) => ({
  ...(await importOriginal<typeof FileViewerContextModule>()),
  useOpenGithubTab: () => openGithubTabMock,
}));

function setComposerGitStatus(overrides: Record<string, unknown> = {}) {
  Object.assign(
    composerGitStatusSnapshot,
    {
      branch: null,
      branchState: "unknown",
      isWorktree: null,
      worktreePath: null,
      creationBranch: null,
      repoNameWithOwner: "omnigent-ai/omnigent",
      githubState: "ready",
      prCount: 0,
      prNumber: null,
      refreshing: false,
    },
    overrides,
  );
}

afterEach(() => setComposerGitStatus());
// SubagentTaskIndicator's child-session query also needs a QueryClient; stub it
// so the indicator self-hides (no active children) in isolated composer renders.
const { childSessionsArgsSpy, composerChildSessions } = vi.hoisted(() => ({
  childSessionsArgsSpy: vi.fn(),
  composerChildSessions: { children: [] as ChildSessionInfo[] },
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: (conversationId: string | null) => {
    childSessionsArgsSpy(conversationId);
    return { children: composerChildSessions.children, isLoading: false, error: null };
  },
}));
// HostBadge now renders in the composer's status-line tray and reads the
// session's host binding via TanStack Query. Stub the hooks so it self-hides
// (no host bound) without needing a QueryClient provider around these renders.
const { composerSnapshotHost } = vi.hoisted(() => ({
  composerSnapshotHost: { id: null as string | null },
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({
    session: { hostId: composerSnapshotHost.id },
    isLoading: false,
    error: null,
  }),
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
import { Composer, computeIsWorking, shouldQueueSend } from "./ChatPage";
import { appendPromptHistoryEntry } from "@/hooks/usePromptHistory";
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
    sendDisabledReason: null,
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
  if (!screen.queryByTestId("composer-agent-menu")) openSessionConfig();
  fireEvent.click(screen.getByTestId("composer-agent-edit"));
  await screen.findByTestId("composer-agent-config-menu");
}

async function openSessionEfforts() {
  if (!screen.queryByTestId("composer-agent-menu")) openSessionConfig();
  fireEvent.click(screen.getByTestId("composer-agent-effort-select"));
  await screen.findByTestId("composer-agent-effort-menu");
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

  it("preserves an unfinished draft when a temporary session receives its real id", async () => {
    useChatStore.setState({ conversationId: "temp:draft" });
    render(<Composer {...composerProps()} />);

    fireEvent.change(textarea(), { target: { value: "draft during startup" } });
    await waitFor(() => expect(getSessionDraft("temp:draft")?.text).toBe("draft during startup"));

    act(() => useChatStore.setState({ conversationId: "conv_real" }));

    await waitFor(() => expect(textarea()).toHaveValue("draft during startup"));
    expect(getSessionDraft("temp:draft")).toBeUndefined();
    expect(getSessionDraft("conv_real")?.text).toBe("draft during startup");
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
    setComposerState({
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

  it.each([
    [false, "metaKey"],
    [false, "ctrlKey"],
    [true, "metaKey"],
    [true, "ctrlKey"],
  ] as const)(
    "steers the draft and backlog (alternate send: %s, modifier: %s)",
    (alternate, modifier) => {
      localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, String(alternate));
      const onSend = vi.fn((text: string, files?: File[]) => {
        useChatStore.getState().enqueueMessage(text, files);
      });
      const sendQueued = vi.fn().mockResolvedValue(undefined);
      const originalState = useChatStore.getState();
      setComposerState({
        boundAgentId: "agent_shortcut",
        queuedMessages: [
          { queueId: "q_1", text: "queued first", conversationId: "conv_shortcut" },
          { queueId: "q_2", text: "queued second", conversationId: "conv_shortcut" },
        ],
        sessionStatus: "running",
        send: sendQueued,
        status: "streaming",
      });

      try {
        renderWithTooltips(
          <Composer {...composerProps({ isWorking: true, onSend, status: "streaming" })} />,
        );
        if (alternate) {
          fireEvent.change(textarea(), { target: { value: "normal draft" } });
          fireEvent.keyDown(textarea(), { key: "Enter", [modifier]: true });
          expect(sendQueued).not.toHaveBeenCalled();
          expect(useChatStore.getState().queuedMessages.map((message) => message.text)).toEqual([
            "queued first",
            "queued second",
            "normal draft",
          ]);
        }
        fireEvent.change(textarea(), { target: { value: "draft last" } });
        fireEvent.keyDown(textarea(), { key: "Enter", [modifier]: true, shiftKey: alternate });

        expect(onSend).toHaveBeenCalledWith("draft last", undefined);
        expect(sendQueued.mock.calls.map((call) => call.slice(0, 2))).toEqual([
          ["queued first", "agent_shortcut"],
          ["queued second", "agent_shortcut"],
          ...(alternate ? [["normal draft", "agent_shortcut"]] : []),
          ["draft last", "agent_shortcut"],
        ]);
        expect(onSend.mock.invocationCallOrder[0]).toBeLessThan(
          sendQueued.mock.invocationCallOrder[0]!,
        );
        expect(useChatStore.getState().queuedMessages).toEqual([]);
      } finally {
        act(() =>
          useChatStore.setState({
            boundAgentId: originalState.boundAgentId,
            queuedMessages: [],
            send: originalState.send,
            sessionStatus: originalState.sessionStatus,
            status: originalState.status,
          }),
        );
      }
    },
  );

  it.each([
    [false, "{Shift>}{Enter}{/Shift}"],
    [true, "{Enter}"],
    [true, "{Shift>}{Enter}{/Shift}"],
  ] as const)("preserves newline input (alternate send: %s, keys: %s)", async (alternate, keys) => {
    localStorage.setItem(COMPOSER_SEND_SHORTCUT_STORAGE_KEY, String(alternate));
    const onSend = vi.fn();
    const user = userEvent.setup();
    render(<Composer {...composerProps({ onSend })} />);
    await user.type(textarea(), "first" + keys + "second");
    expect(textarea().value).toBe("first\nsecond");
    expect(onSend).not.toHaveBeenCalled();
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
    setComposerState({
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
    setComposerState({
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

  it.each([
    { kind: "claude", reset: false },
    { kind: "opencode", reset: true },
    { kind: "acp", reset: true },
    { kind: "configured", reset: true },
    { kind: null, reset: true },
    { kind: null, reset: true, terminalFirst: true, harness: "claude-sdk" },
    { kind: "codex", reset: false },
    { kind: "pi", reset: false },
    { kind: "cursor", reset: false },
    { kind: "kiro", reset: false },
    { kind: "devin", reset: false },
    { kind: "codex", reset: true, configured: true },
    { kind: "claude", reset: true, configured: true },
  ] as const)("gates model reset consistently for $kind ($reset, $configured)", async (row) => {
    const setModel = vi.fn().mockResolvedValue(undefined);
    const onSend = vi.fn();
    useChatStore.setState({ setModel, sessionHarness: "harness" in row ? row.harness : null });
    renderWithTooltips(
      <Composer
        {...composerProps({
          onSend,
          showEffort: true,
          showModels: row.kind !== null,
          modelPickerKind: row.kind,
          isTerminalFirst: "terminalFirst" in row && row.terminalFirst,
          inferenceConfigured: "configured" in row && row.configured,
          isNativeWrapper: row.kind !== null && row.kind !== "acp" && row.kind !== "configured",
          codexModelOptions: [{ id: "primary", displayName: "Primary", isDefault: true }],
        })}
      />,
    );
    const ta = textarea();
    fireEvent.change(ta, { target: { value: "/mod" } });
    expect(screen.getByTestId("slash-menu-item-model").textContent?.includes("default")).toBe(
      row.reset,
    );

    fireEvent.change(ta, { target: { value: "/help " } });
    fireEvent.keyDown(ta, { key: "Enter" });
    const help = screen.getByText(/\/model — Switch the model/);
    expect(help).toHaveTextContent("/model <name>");
    expect(help.textContent?.includes("/model <name> | default")).toBe(row.reset);
    expect(help).toHaveTextContent("/effort low | medium | high | default");

    for (const alias of ["default", "off", "reset", "DEFAULT"]) {
      setModel.mockClear();
      fireEvent.change(ta, { target: { value: `/model ${alias}` } });
      fireEvent.keyDown(ta, { key: "Enter" });
      expect(onSend).not.toHaveBeenCalled();
      if (row.reset) {
        expect(setModel).toHaveBeenCalledWith(null, expect.anything());
        expect(ta).toHaveValue("");
      } else {
        expect(setModel).not.toHaveBeenCalled();
        expect(screen.getByText(/This session does not support resetting/)).toBeVisible();
        expect(ta).toHaveValue(`/model ${alias}`);
      }
    }
    setModel.mockClear();
    fireEvent.change(ta, { target: { value: "/model primary" } });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(setModel).toHaveBeenCalledWith("primary", expect.anything());

    if (row.kind !== null) {
      await openSessionModels();
      expect(screen.queryByRole("menuitem", { name: "Use default model" })).toBeNull();
      expect(screen.queryByRole("menuitemcheckbox", { name: "Default" })).toBeNull();
      setModel.mockClear();
      fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Primary" }));
      await waitFor(() =>
        expect(setModel).toHaveBeenCalledWith(row.reset ? null : "primary", expect.anything()),
      );
    }
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

describe("Composer cached model labels", () => {
  const model = "provider/model-a";
  const catalog = [{ id: "alias-a", model, displayName: "Team model" }];
  const scope = {
    sessionId: "conv_cached_label",
    hostId: "host-a",
    agentId: "agent-a",
    harness: "claude-native",
  };
  const props = () =>
    composerProps({
      modelPickerKind: "claude",
      showModels: true,
      showEffort: false,
      codexModelOptions: catalog,
      modelLabelOptions: [],
    });
  beforeEach(() => {
    localStorage.clear();
    composerSnapshotHost.id = scope.hostId;
    vi.spyOn(host, "getOmnigentServerIdentity").mockReturnValue("server-a");
    vi.spyOn(identity, "getCurrentUserId").mockReturnValue("user-a");
    setComposerState({
      conversationId: scope.sessionId,
      sessionHostId: scope.hostId,
      boundAgentId: scope.agentId,
      sessionHarness: scope.harness,
      llmModel: model,
      sessionModelOverride: null,
      sessionModelSeeded: false,
      pendingModelChange: null,
      nativeVendorOwnsModel: false,
      costControlModeOverride: "off",
      skills: [],
    });
  });
  afterEach(() => {
    cleanup();
    composerSnapshotHost.id = null;
    vi.restoreAllMocks();
    localStorage.clear();
    useChatStore.setState({ sessionModelSeeded: false, sessionHostId: null, boundAgentId: null });
  });

  it("uses host names for the label and menu until session metadata arrives", async () => {
    const view = renderWithTooltips(<Composer {...props()} />);
    const trigger = screen.getByTestId("composer-config-gear");
    expect(screen.queryByRole("status", { name: "Loading model" })).toBeNull();
    expect(trigger).toBeEnabled();
    expect(trigger).not.toHaveTextContent(model);
    expect(trigger).toHaveTextContent("Team model");
    expect(readSessionModelLabelCache(getSessionModelLabelCacheKey(scope, model))).toBeNull();
    fireEvent.focus(trigger);
    const tooltip = await screen.findByTestId("composer-config-gear-tooltip");
    expect(tooltip).toHaveTextContent("Team model");
    expect(tooltip).not.toHaveTextContent(model);
    openSessionConfig();
    expect(screen.getByTestId("composer-agent-model-summary")).toHaveTextContent("Team model");
    fireEvent.click(screen.getByTestId("composer-agent-edit"));
    expect(await screen.findByTestId("composer-agent-model-alias-a")).toBeEnabled();

    view.rerender(
      <TooltipProvider>
        <Composer
          {...props()}
          modelLabelOptions={[{ ...catalog[0], displayName: "Session name" }]}
        />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId("composer-model-loading")).toBeNull();
    expect(trigger).toHaveTextContent("Session name");
  });

  it("uses the cached session display name immediately on remount, not a newer host probe", async () => {
    const first = renderWithTooltips(<Composer {...props()} modelLabelOptions={catalog} />);
    expect(readSessionModelLabelCache(getSessionModelLabelCacheKey(scope, model))).toBe(
      "Team model",
    );
    first.unmount();
    useChatStore.setState({ sessionHostId: null });
    renderWithTooltips(
      <Composer {...props()} codexModelOptions={[{ ...catalog[0], displayName: "Host name" }]} />,
    );
    const trigger = screen.getByTestId("composer-config-gear");
    expect(trigger).toHaveTextContent("Team model");
    expect(trigger).not.toHaveTextContent("Host name");
    expect(screen.queryByTestId("composer-model-loading")).toBeNull();
    await openSessionModels();
    expect(screen.getByTestId("composer-agent-model-alias-a")).toBeEnabled();
  });

  it("names the reported Codex model from the host catalog without changing the requested model", async () => {
    useChatStore.setState({
      sessionHarness: "codex-native",
      sessionModelOverride: "another-model",
    });
    renderWithTooltips(<Composer {...props()} modelPickerKind="codex" />);
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Team model");
    expect(screen.queryByTestId("composer-model-loading")).toBeNull();
    await openSessionModels();
    expect(screen.getByTestId("composer-agent-model-alias-a")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(useChatStore.getState().llmModel).toBe(model);
    expect(useChatStore.getState().sessionModelOverride).toBe("another-model");
  });

  it("uses a loading label for the synthetic current row without a catalog", async () => {
    renderWithTooltips(<Composer {...props()} codexModelOptions={[]} />);
    await openSessionModels();
    expect(
      screen.getByRole("menuitemcheckbox", { name: "Loading model… (current)" }),
    ).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("composer-agent-config-menu")).not.toHaveTextContent(model);
  });

  it("does not reuse the creation host's cache after the snapshot host changes", () => {
    const first = renderWithTooltips(<Composer {...props()} modelLabelOptions={catalog} />);
    composerSnapshotHost.id = "new-host";
    first.rerender(
      <TooltipProvider>
        <Composer {...props()} codexModelOptions={[]} />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("composer-model-loading")).toBeInTheDocument();
    expect(screen.getByTestId("composer-config-gear")).not.toHaveTextContent("Team model");
  });

  it("does not cache an optimistic creation seed until the runner confirms it", () => {
    useChatStore.setState({ sessionModelSeeded: true, sessionModelOverride: model });
    renderWithTooltips(<Composer {...props()} modelLabelOptions={catalog} />);
    const key = getSessionModelLabelCacheKey(scope, model);
    expect(readSessionModelLabelCache(key)).toBeNull();
    act(() => useChatStore.setState({ sessionModelSeeded: false }));
    expect(readSessionModelLabelCache(key)).toBe("Team model");
  });

  it("keeps Smart Routing visible without a label-loading spinner", () => {
    useChatStore.setState({ costControlModeOverride: "on" });
    renderWithTooltips(<Composer {...props()} costRoutingEligible />);
    expect(screen.getByTestId("composer-config-gear")).toHaveTextContent("Smart Routing");
    expect(screen.queryByTestId("composer-model-loading")).toBeNull();
  });
});

describe("Composer model/effort label", () => {
  beforeEach(() => {
    setComposerState({
      conversationId: "conv_test",
      skills: [],
      sessionModelOverride: null,
      sessionReasoningEffort: null,
      llmModel: null,
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

  it("opens directly to Model and Effort rows under the session harness heading", () => {
    useChatStore.setState({
      llmModel: "system.ai.claude-opus-4-6",
      sessionHarness: "claude-native",
      sessionReasoningEffort: "xhigh",
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
    const menu = within(screen.getByTestId("composer-agent-menu"));
    expect(menu.getByText("Claude Code")).toBeVisible();
    expect(menu.queryByText("Harnesses")).toBeNull();
    expect(menu.queryByText("Edit")).toBeNull();
    expect(menu.getAllByRole("menuitem")).toHaveLength(2);
    const row = menu.getByRole("menuitem", { name: "Model: Opus" });
    expect(within(row).getByText("Opus")).toHaveClass("text-right");
    expect(menu.getByRole("menuitem", { name: "Effort: xHigh" })).toBeVisible();
    expect(screen.queryByRole("menuitemcheckbox")).toBeNull();
    fireEvent.keyDown(row, { key: "ArrowRight" });
    expect(screen.getByTestId("composer-agent-model-opus")).toHaveTextContent("Opus");
    expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
  });

  it("shows the model in the foreground and effort muted", () => {
    // The chip renders the harness's reported model (`llmModel`), never the
    // sticky preference or the request.
    useChatStore.setState({ llmModel: "opus", sessionReasoningEffort: "high" });
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

  it("shows no effort when the session uses its default", () => {
    useChatStore.setState({
      llmModel: "opus",
      sessionReasoningEffort: null,
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
  });

  it("reads 'Smart Routing' with no model/effort when routing is on", async () => {
    // The router picks model + effort per turn, so the label must not surface a
    // stale pinned model/effort — it reads "Smart Routing" instead.
    useChatStore.setState({
      sessionModelOverride: "opus",
      sessionReasoningEffort: "high",
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

  it("renders the reported model, never the request", () => {
    useChatStore.setState({
      sessionModelOverride: "sonnet",
      sessionReasoningEffort: null,
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
  });

  it("keeps the harness visible when model and effort are unresolved", () => {
    // A claude-native session before the snapshot fills llmModel/sessionReasoningEffort
    // has no model label and no effort label. The read-only label renders
    // nothing rather than a placeholder — the gear still owns the config path.
    useChatStore.setState({
      sessionModelOverride: null,
      sessionReasoningEffort: null,
      llmModel: null,
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
    expect(screen.getByTestId("composer-agent-config-value")).toHaveTextContent("Claude Code");
    // The gear is still present so the user can open the config modal.
    expect(screen.getByTestId("composer-config-gear")).toBeTruthy();
  });

  it("falls back to the harness identity for an SDK/bundle agent with no model/effort", () => {
    // Polly (claude-sdk/pi bundle) surfaces no model or effort, so the label
    // would be empty. It falls back to the harness identity ("Polly (Pi)") so
    // the slot isn't blank.
    useChatStore.setState({
      sessionModelOverride: null,
      sessionReasoningEffort: null,
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
      sessionModelOverride: null,
      sessionReasoningEffort: null,
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
    useChatStore.setState({
      sessionModelOverride: null,
      sessionReasoningEffort: null,
      llmModel: null,
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
    expect(screen.getByTestId("composer-agent-config-value")).toHaveTextContent("Claude Code");
  });

  it("surfaces a cursor-native session's model from the override", () => {
    // cursor-native is a vendor-owns-model wrapper, so `nativeVendorOwnsModel`
    // is true and the bound `llmModel` is a meaningless default. Its live model
    // is mirrored into the session override (`sessionModelOverride`). The label
    // must read the real session model ("Composer 2.5").
    useChatStore.setState({
      nativeVendorOwnsModel: true,
      sessionModelOverride: "composer-2.5",
      sessionReasoningEffort: "low",
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

  it("surfaces an SDK/bundle session's model from the override", () => {
    useChatStore.setState({
      sessionModelOverride: "claude-opus-4-8",
      sessionReasoningEffort: null,
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

  it("keeps the model label empty when an SDK/bundle session has no applied model", () => {
    useChatStore.setState({
      sessionModelOverride: null,
      sessionReasoningEffort: "high",
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

  it("waits for a native session model before its catalog lands", () => {
    // During a Codex→Claude switch, `switchTo` clears the session-scoped model
    // fields. The label must wait for a model this session vouches for.
    useChatStore.setState({
      sessionModelOverride: null,
      sessionReasoningEffort: "high",
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
    useChatStore.setState({ llmModel: "opus", sessionReasoningEffort: "high" });
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
    expect(screen.getByTestId("composer-agent-model-opus")).toBeInTheDocument();
  });

  it("keeps the label click inert when the session is read-only", () => {
    // Mirrors the gear's soft-disable: a read-only viewer gets no config modal
    // from either pill half, and aria-disabled drops the pill's hover
    // highlight so no dead affordance is advertised.
    useChatStore.setState({ llmModel: "opus", sessionReasoningEffort: "high" });
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
      sessionModelOverride: null,
      sessionReasoningEffort: null,
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
    childSessionsArgsSpy.mockClear();
    composerChildSessions.children = [];
    useChatStore.setState({
      contextWindow: null,
      tokensUsed: null,
      backgroundTaskCount: 0,
      backgroundTasks: [],
    });
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
      sessionReasoningEffort: null,
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
    expect(textarea().closest("form")).toHaveClass("pb-[max(20px,env(safe-area-inset-bottom))]");
    // A normal working directory has no empty worktree affordance.
    expect(within(workspace).queryByTestId("composer-git-branch")).toBeNull();
    expect(screen.getByTestId("composer-host-select")).toHaveClass("w-11", "md:h-7");
    expect(screen.getByTestId("composer-permission-chip")).toHaveTextContent("Ask for approval");
    const trigger = screen.getByTestId("composer-config-gear");
    expect(trigger.querySelector("img")).toBeTruthy();
    expect(trigger.querySelector(".lucide-settings")).toBeNull();
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getByTestId("composer-agent-menu")).toBeInTheDocument();
    expect(screen.queryByTestId("composer-config-modal")).toBeNull();
  });

  it("transitions PR and worktree visibility across ready, empty, loading, and unknown GitHub states", () => {
    setComposerGitStatus({
      branch: "feature/shared-composer",
      branchState: "branch",
      isWorktree: true,
      worktreePath: "/home/alice/repo-wt/feature",
      githubState: "ready",
      repoNameWithOwner: "omnigent-ai/omnigent",
      prCount: 1,
      prNumber: 42,
    });
    const view = renderWithTooltips(<Composer {...composerProps()} />);
    expect(screen.getByTestId("composer-pr-link")).toHaveTextContent("#42");
    expect(screen.getByTestId("composer-git-branch")).toHaveTextContent("feature/shared-composer");

    setComposerGitStatus({
      branch: "feature/shared-composer",
      branchState: "branch",
      isWorktree: true,
      worktreePath: "/home/alice/repo-wt/feature",
      githubState: "ready",
      repoNameWithOwner: "omnigent-ai/omnigent",
      prCount: 0,
      prNumber: null,
    });
    view.rerender(
      <TooltipProvider>
        <Composer {...composerProps()} />
      </TooltipProvider>,
    );
    expect(screen.queryByTestId("composer-pr-link")).toBeNull();
    expect(screen.getByTestId("composer-git-branch")).toBeInTheDocument();

    setComposerGitStatus({ githubState: "loading", repoNameWithOwner: null });
    view.rerender(
      <TooltipProvider>
        <Composer {...composerProps()} />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("composer-pr-loading")).toHaveTextContent("Checking PR…");
    expect(screen.queryByTestId("composer-git-branch")).toBeNull();

    setComposerGitStatus({ githubState: "unknown" });
    view.rerender(
      <TooltipProvider>
        <Composer {...composerProps()} />
      </TooltipProvider>,
    );
    expect(screen.getByTestId("composer-pr-unknown")).toHaveTextContent("PR unavailable");
    expect(screen.queryByTestId("composer-git-branch")).toBeNull();
  });

  it("keeps the PR to the left of the confirmed worktree status", () => {
    setComposerGitStatus({
      branch: "feature/shared-composer",
      branchState: "branch",
      isWorktree: true,
      worktreePath: "/home/alice/repo-wt/feature",
      githubState: "ready",
      repoNameWithOwner: "omnigent-ai/omnigent",
      prCount: 1,
      prNumber: 42,
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    const pr = screen.getByTestId("composer-pr-link");
    const worktree = screen.getByTestId("composer-git-branch");
    expect(pr.compareDocumentPosition(worktree) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(0);
  });

  it("mounts sub-agent work after context and background indicators with navigation", () => {
    useChatStore.setState({
      conversationId: "conv_parent",
      contextWindow: 100_000,
      tokensUsed: 25_000,
      backgroundTaskCount: 1,
      backgroundTasks: [],
    });
    composerChildSessions.children = [
      {
        id: "conv_child",
        title: "developer:queue-tests",
        task_summary: "Verify queue behavior",
        tool: "developer",
        session_name: "queue-tests",
        labels: {},
        current_task_status: "in_progress",
        last_task_error: null,
        busy: true,
        last_message_preview: null,
        pending_elicitations_count: 0,
        routed_model: null,
      },
    ];

    render(
      <MemoryRouter initialEntries={["/c/conv_parent?file=README.md&debug=1"]}>
        <TooltipProvider>
          <Composer {...composerProps()} />
        </TooltipProvider>
      </MemoryRouter>,
    );

    const workspace = screen.getByTestId("composer-workspace-controls");
    const context = within(workspace).getByTestId("composer-context-ring");
    const background = within(workspace).getByTestId("background-task-pill");
    const subagent = within(workspace).getByTestId("subagent-task-pill");
    expect(context.compareDocumentPosition(background) & Node.DOCUMENT_POSITION_FOLLOWING).not.toBe(
      0,
    );
    expect(
      background.compareDocumentPosition(subagent) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).not.toBe(0);
    expect(childSessionsArgsSpy).toHaveBeenCalledWith("conv_parent");

    fireEvent.click(subagent);
    expect(
      screen.getByRole("link", { name: /Verify queue behavior.*Working.*developer/ }),
    ).toHaveAttribute("href", "/c/conv_child?debug=1");
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

describe("Composer background tasks", () => {
  beforeEach(() => {
    clearSessionDrafts();
    setComposerState({
      conversationId: "conv_background_tasks",
      sessionHarness: "claude-native",
      backgroundTaskCount: 0,
      backgroundTasks: [],
      skills: [],
    });
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({ backgroundTaskCount: 0, backgroundTasks: [] });
    clearSessionDrafts();
  });

  it("shows a running monitor in the workspace bar after foreground work ends", () => {
    useChatStore.setState({
      backgroundTaskCount: 1,
      backgroundTasks: [
        {
          id: "monitor-ci",
          type: "shell",
          status: "running",
          description: "Watch PR checks",
          command: "gh pr checks 123 --watch",
        },
      ],
    });
    renderWithTooltips(<Composer {...composerProps()} />);

    const workspace = screen.getByTestId("composer-workspace-controls");
    const trigger = within(workspace).getByRole("button", {
      name: "1 background task still running",
    });
    expect(trigger).toHaveTextContent("1");
    expect(screen.queryByTestId("subagent-task-pill")).toBeNull();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "1 background task" });
    expect(dialog).toHaveTextContent("Watch PR checks");
    expect(dialog).toHaveTextContent("gh pr checks 123 --watch");
  });

  it("shows live task counts and removes the control when all tasks finish", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(screen.queryByTestId("background-task-pill")).toBeNull();

    act(() => useChatStore.setState({ backgroundTaskCount: 2 }));
    expect(
      screen.getByRole("button", { name: "2 background tasks still running" }),
    ).toHaveTextContent("2");

    act(() => useChatStore.setState({ backgroundTaskCount: 0 }));
    expect(screen.queryByTestId("background-task-pill")).toBeNull();
  });

  it("lets one outside click focus the composer and resume typing", async () => {
    const user = userEvent.setup();
    const props = composerProps();
    useChatStore.setState({ backgroundTaskCount: 1 });
    renderWithTooltips(<Composer {...props} />);

    await user.click(screen.getByRole("button", { name: "1 background task still running" }));
    expect(screen.getByRole("dialog", { name: "1 background task" })).toBeVisible();
    await user.click(textarea());
    expect(screen.queryByRole("dialog", { name: "1 background task" })).toBeNull();
    expect(textarea()).toHaveFocus();
    await user.keyboard("Keep monitoring");
    expect(textarea()).toHaveValue("Keep monitoring");
    expect(props.onSend).not.toHaveBeenCalled();
  });
});

describe("Composer effort slash-command visibility", () => {
  beforeEach(() => {
    setComposerState({ conversationId: "conv_test", skills: [] });
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
    setComposerState({
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

describe("Composer native skill menu", () => {
  beforeEach(() => {
    clearSessionDrafts();
    setComposerState({
      conversationId: "conv_skill_menu",
      sessionHarness: "codex-native",
      skills: [{ name: "review", description: "Review the current change" }],
      skillsStatus: "ready",
    });
  });
  afterEach(() => {
    cleanup();
    clearSessionDrafts();
    setComposerState({ sessionHarness: null, skills: [], skillsStatus: null });
  });

  it.each([
    { trigger: "$", selection: "Tab" },
    { trigger: "/", selection: "click" },
  ])("opens with $trigger and selects a skill with $selection", ({ trigger, selection }) => {
    const props = composerProps({ isNativeWrapper: true });
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: trigger } });
    expect(screen.getByTestId("slash-menu-item-help")).toHaveTextContent("/help");
    expect(screen.getByTestId("slash-menu-item-review")).toHaveTextContent("$review");

    fireEvent.change(textarea(), { target: { value: `${trigger}rev` } });
    if (selection === "click") {
      fireEvent.click(screen.getByTestId("slash-menu-item-review"));
    } else {
      fireEvent.keyDown(textarea(), { key: "Tab" });
    }
    expect(textarea()).toHaveValue("$review ");
    expect(props.onSend).not.toHaveBeenCalled();
    expect(screen.queryByTestId("slash-menu-item-review")).toBeNull();

    fireEvent.change(textarea(), { target: { value: "$review focus on tests" } });
    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay).toHaveTextContent("$review focus on tests");
    expect(overlay.querySelector(".text-brand-accent")?.textContent).toBe("$review");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledExactlyOnceWith("$review focus on tests", undefined);
  });

  it("keeps built-in commands slash-prefixed when opened with a dollar sign", () => {
    const props = composerProps({ isNativeWrapper: true });
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "$eff" } });
    expect(screen.getByTestId("slash-menu-item-effort")).toHaveTextContent("/effort");
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(textarea()).toHaveValue("/effort ");
    expect(props.onSend).not.toHaveBeenCalled();
  });

  it.each(["claude-native", "claude-sdk"])(
    "keeps slash-only skill completion for %s",
    (harness) => {
      useChatStore.setState({ sessionHarness: harness });
      render(<Composer {...composerProps({ isNativeWrapper: harness === "claude-native" })} />);
      fireEvent.change(textarea(), { target: { value: "$rev" } });
      expect(screen.queryByTestId("slash-menu-item-review")).toBeNull();
      expect(screen.queryByTestId("composer-highlight-overlay")).toBeNull();

      fireEvent.change(textarea(), { target: { value: "/rev" } });
      fireEvent.keyDown(textarea(), { key: "Tab" });
      expect(textarea()).toHaveValue("/review ");
    },
  );
});

describe("Composer asynchronous skills", () => {
  beforeEach(() => {
    clearSessionDrafts();
    setComposerState({
      conversationId: "conv_loading_skills",
      skills: [],
      skillsStatus: "loading",
      terminalPending: false,
    });
  });
  afterEach(() => {
    cleanup();
    clearSessionDrafts();
    setComposerState({ skills: [], skillsStatus: null, terminalPending: false });
    vi.useRealTimers();
  });

  it.each([
    { runnerStarting: true, terminalPending: false },
    { runnerStarting: false, terminalPending: true },
  ])("waits for skills while the session starts: %j", ({ runnerStarting, terminalPending }) => {
    setComposerState({ skillsStatus: "unavailable", terminalPending });
    const props = composerProps({ runnerStarting });
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "/review" } });
    expect(screen.getByText("Loading skills…")).toBeVisible();
    expect(screen.queryByText("Skills unavailable while disconnected.")).toBeNull();
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).not.toHaveBeenCalled();
    act(() =>
      setComposerState({
        skills: [{ name: "code-review", description: "Review code" }],
        skillsStatus: "ready",
      }),
    );
    expect(screen.queryByText("Loading skills…")).toBeNull();
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(textarea()).toHaveValue("/code-review ");
  });

  it("stops showing startup loading when the runner stays disconnected", () => {
    setComposerState({ skillsStatus: "unavailable" });
    const props = composerProps({ runnerStarting: true });
    const { rerender } = render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    expect(screen.getByText("Loading skills…")).toBeVisible();
    rerender(<Composer {...props} runnerStarting={false} />);
    expect(screen.queryByText("Loading skills…")).toBeNull();
    expect(screen.getByText("Skills unavailable while disconnected.")).toBeVisible();
  });

  it("shows discovery errors even when the session is still starting", () => {
    setComposerState({ skillsStatus: "error" });
    render(<Composer {...composerProps({ runnerStarting: true })} />);
    fireEvent.change(textarea(), { target: { value: "/review" } });
    expect(screen.queryByText("Loading skills…")).toBeNull();
    expect(screen.getByText("Couldn’t load skills.")).toBeVisible();
  });

  it("dismisses a loading-only menu before interrupting a running session", () => {
    const props = composerProps({ isWorking: true });
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "/review" } });
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(props.onStop).not.toHaveBeenCalled();
    expect(textarea()).toHaveValue("");
    expect(screen.queryByText("Loading skills…")).toBeNull();
  });

  it("retries the host catalog directly", () => {
    setComposerState({ skillsStatus: "error" });
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "/review" } });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(skillsFixture.getState().refetch).toHaveBeenCalledOnce();
  });

  it("preserves the highlighted command when skills arrive", () => {
    render(<Composer {...composerProps()} />);
    fireEvent.change(textarea(), { target: { value: "/" } });
    fireEvent.keyDown(textarea(), { key: "ArrowDown" });
    const selected = activeRow()?.textContent;
    act(() =>
      setComposerState({
        skills: [{ name: "review", description: "Review code" }],
        skillsStatus: "ready",
      }),
    );
    expect(screen.getByTestId("slash-menu-item-review")).toBeVisible();
    expect(activeRow()?.textContent).toBe(selected);
    expect(screen.queryByText("Loading skills…")).toBeNull();
  });

  it("waits for completion instead of sending a partial skill name", () => {
    const props = composerProps();
    render(<Composer {...props} />);
    fireEvent.change(textarea(), { target: { value: "/review" } });
    expect(screen.getByText("Loading skills…")).toBeVisible();
    fireEvent.keyDown(textarea(), { key: "Enter" });
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(props.onSend).not.toHaveBeenCalled();
    expect(textarea()).toHaveValue("/review");
    act(() =>
      setComposerState({
        skills: [{ name: "code-review", description: "Review code" }],
        skillsStatus: "ready",
      }),
    );
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(textarea()).toHaveValue("/code-review ");
    expect(props.onSend).not.toHaveBeenCalled();
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
    // Row testids (not text): each row renders its description inline too, so a
    // text query could match a description as well as a name.
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

  it("shows each entry's description inline on its row", () => {
    render(<SlashCommandMenu query="" activeIndex={1} onSelect={vi.fn()} commands={COMMANDS} />);
    // Descriptions render inline on each row (grouped "+"-tray style), so a
    // skill's blurb is always visible — not hidden behind a highlight in a
    // separate detail card.
    const beta = screen.getByTestId("slash-menu-item-beta");
    expect(beta.textContent).toContain("/beta");
    expect(beta.textContent).toContain("Second");
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
    setComposerState({ conversationId: "conv_test", skills: [] });
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

  it("keeps drafting enabled while session creation blocks submission", () => {
    const props = composerProps({
      disabled: true,
      unreachable: true,
      permissionLevel: 1,
      sendDisabledReason: "Starting the session…",
    });
    render(<Composer {...props} />);

    const input = textarea();
    expect(input).toBeEnabled();
    expect(input.placeholder).toMatch(/send a message/i);
    fireEvent.change(input, { target: { value: "queue this next" } });
    expect(input).toHaveValue("queue this next");
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();

    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onSend).not.toHaveBeenCalled();
    expect(input).toHaveValue("queue this next");
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
    setComposerState({ conversationId: "conv_test", skills: [] });
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

  it("keeps Interrupt available and preserves a draft typed during a pending elicitation", () => {
    useChatStore.setState({ conversationId: "conv_interrupt_draft", blocks: [elicitationBlock()] });
    const onStop = vi.fn();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ isWorking: true, onStop, onSend })} />);

    fireEvent.change(textarea(), { target: { value: "keep this draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Interrupt" }));

    expect(onStop).toHaveBeenCalledTimes(1);
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea()).toHaveValue("keep this draft");
  });

  it("keeps ChatPage's waiting elicitation interruptible", () => {
    // A pending request blocks sending but keeps the waiting turn interruptible.
    useChatStore.setState({ conversationId: "conv_wiring", blocks: [elicitationBlock()] });
    const isWorking = computeIsWorking("waiting");
    render(<Composer {...composerProps({ isWorking, status: "streaming" })} />);
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
    setComposerState({
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

describe("Composer startSideChat (text-select → Ask in side chat)", () => {
  beforeEach(() => {
    clearSessionDrafts();
    localStorage.clear();
    setComposerState({
      conversationId: "conv_test",
      skills: [],
      blocks: [],
      failedSendDraft: null,
      queuedMessages: [],
      sessionHarness: "codex-native",
    });
  });

  afterEach(() => {
    cleanup();
    clearSessionDrafts();
    vi.restoreAllMocks();
  });

  it("adds the selection as a quote card and flags it a side chat", () => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps()} ref={ref} />);

    act(() => ref.current?.startSideChat("restore the row on failure"));

    // Renders exactly like a reply quote (card + empty tail input), plus the
    // side-chat hint so the user knows this will fork.
    expect(textarea()).toHaveValue("");
    expect(
      screen.getByTestId("composer-reply-quote").querySelector("blockquote"),
    ).toHaveTextContent("restore the row on failure");
    expect(screen.getByTestId("composer-side-chat-hint")).toBeInTheDocument();
  });

  it("sends as a /side command carrying the quoted selection and the question", () => {
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...props} ref={ref} />);

    act(() => ref.current?.startSideChat("restore the row on failure"));
    fireEvent.change(textarea(), { target: { value: "why is this safe?" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });

    // Prefixed with /side so the existing pipeline forks it; no reply-draft
    // snapshot (a side chat keeps no main-chat bubble).
    expect(props.onSend).toHaveBeenCalledWith(
      "/side > restore the row on failure\n\nwhy is this safe?",
      undefined,
    );
    expect(textarea()).toHaveValue("");
  });

  it("falls back to a normal reply when the session has no side-chat harness", () => {
    // Side chat is generic now (every harness supports it), so the only
    // no-side-chat case is a session with no bound harness — then a quoted
    // "Ask in side chat" degrades to an ordinary reply.
    useChatStore.setState({ sessionHarness: "" });
    const props = composerProps();
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...props} ref={ref} />);

    act(() => ref.current?.startSideChat("some selection"));
    fireEvent.change(textarea(), { target: { value: "a question" } });
    fireEvent.keyDown(textarea(), { key: "Enter" });

    // No /side prefix, no fork; sends as an ordinary quoted reply with its snapshot.
    expect(props.onSend).toHaveBeenCalledWith("> some selection\n\na question", undefined, {
      version: 1,
      quotes: [{ before: "", text: "some selection" }],
      text: "a question",
    });
  });

  it.each([
    { disabled: true },
    { permissionLevel: 1 },
    { readOnlyReason: "Read-only session" },
    { unreachable: true },
  ])("does nothing on a disabled composer: %j", (overrides) => {
    const ref = createRef<ComponentRef<typeof Composer>>();
    render(<Composer {...composerProps(overrides)} ref={ref} />);
    act(() => ref.current?.startSideChat("selected text"));
    expect(textarea()).toHaveValue("");
    expect(screen.queryByTestId("composer-reply-quote")).not.toBeInTheDocument();
  });
});

// Attaching a file via the paperclip button routes through the hidden file
// <input>, whose click (and the OS file dialog) pulls focus off the composer.
// The change handler must hand focus back so the user can keep typing the
// message that goes with the attachment — without this the caret is lost and
// the next keystroke does nothing until the chat box is clicked again.
describe("Composer file-attachment focus", () => {
  beforeEach(() => {
    setComposerState({ conversationId: "conv_test", skills: [] });
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

    // An image attaches as a thumbnail; its filename is the img alt text.
    expect(screen.getByAltText("shot.png")).toBeTruthy();
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
    setComposerState({ conversationId: "conv_test", skills: [] });
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
    expect(screen.getByTestId("composer-workspace-controls")).not.toHaveClass("rounded-t-none");
  });

  // The sub-agent tray sits directly above the workspace bar, sharing its
  // column wrapper and inset so their edges line up.
  it("sits directly above the workspace bar, sharing its column", () => {
    render(<Composer {...composerProps({ subAgentLabel: "check-account-eligibility" })} />);
    const bar = document.querySelector('[data-testid="composer-workspace-controls"]');
    expect(bar).not.toBeNull();
    expect(tray()?.nextElementSibling).toBe(bar);
    expect(tray()?.parentElement).toBe(bar?.parentElement);
  });

  it("keeps the workspace bar's rounded top on a top-level session (no tray)", () => {
    render(<Composer {...composerProps()} />);
    const bar = document.querySelector('[data-testid="composer-workspace-controls"]');
    expect(bar?.className).not.toContain("rounded-t-none");
  });

  it("removes the workspace bar's inner arc when the queue tray is docked above it", () => {
    setComposerState({
      conversationId: "conv_test",
      skills: [],
      queuedMessages: [{ queueId: "q_1", text: "held follow-up", conversationId: "conv_test" }],
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(screen.getByTestId("composer-workspace-controls")).toHaveClass(
      "rounded-t-none",
      "pl-2.5",
      "border-t-0",
      "border-border/50",
      "before:inset-x-4",
      "before:h-px",
    );
  });
});

// The trays peeking above the composer (queued strip, sub-agent tray) dock
// onto the inset workspace bar: a tray's negative-margin tuck only hides its
// bottom corners behind a surface at least as wide, so the trays must share
// the bar's column wrapper and inset. Rendered as full-column siblings of the
// wrapper instead, page background shows under their outer edges and the tray
// floats detached above the composer (real geometry is covered by
// tests/e2e_ui/chat/test_queued_strip_docks_on_composer.py).
describe("Composer trays dock onto the workspace bar", () => {
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    useChatStore.setState({ queuedMessages: [] });
  });

  /** The column wrapper holding the workspace bar. */
  function workspaceBarParent(): Element | null {
    return (
      document.querySelector('[data-testid="composer-workspace-controls"]')?.parentElement ?? null
    );
  }

  it("renders the queued strip inside the workspace bar's column wrapper", () => {
    setComposerState({
      conversationId: "conv_test",
      skills: [],
      queuedMessages: [{ queueId: "q_1", text: "held follow-up", conversationId: "conv_test" }],
    });
    // Tooltip provider for the strip's per-row steer/edit/delete buttons.
    renderWithTooltips(<Composer {...composerProps()} />);
    const strip = document.querySelector('[data-testid="composer-queued-strip"]');
    expect(strip).not.toBeNull();
    expect(workspaceBarParent()).not.toBeNull();
    expect(strip!.parentElement).toBe(workspaceBarParent());
  });

  it("renders the sub-agent tray inside the workspace bar's column wrapper", () => {
    setComposerState({ conversationId: "conv_test", skills: [] });
    render(<Composer {...composerProps({ subAgentLabel: "check-account-eligibility" })} />);
    const tray = document.querySelector('[data-testid="composer-subagent-tray"]');
    expect(tray).not.toBeNull();
    expect(workspaceBarParent()).not.toBeNull();
    expect(tray!.parentElement).toBe(workspaceBarParent());
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

describe("Composer — editing queued messages", () => {
  const CONV = "conv_uparrow_edit";
  const QUEUED_TEXT = "queued follow-up recalled for editing";

  beforeEach(() => {
    clearSessionDrafts();
    // A running turn keeps the flush effect from draining the queue while
    // the test drives the recall-edit journey.
    useChatStore.setState({
      conversationId: CONV,
      boundAgentId: "agent_xyz",
      status: "idle",
      sessionStatus: "running",
      queuedMessages: [{ queueId: "q_1", text: QUEUED_TEXT, conversationId: CONV }],
    });
  });

  afterEach(() => {
    cleanup();
    useChatStore.setState({ queuedMessages: [] });
    // Drop the prompt-history keys these tests seeded.
    for (let i = window.localStorage.length - 1; i >= 0; i--) {
      const key = window.localStorage.key(i);
      if (key?.startsWith("omnigent:prompt-history")) window.localStorage.removeItem(key);
    }
  });

  it("dequeues the recalled message so re-sending can't duplicate it", async () => {
    appendPromptHistoryEntry(QUEUED_TEXT, CONV);
    const props = composerProps({ onSend: useChatStore.getState().enqueueMessage });
    renderWithTooltips(<Composer {...props} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    await waitFor(() => expect(textarea().value).toBe(QUEUED_TEXT));
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
    fireEvent.change(textarea(), { target: { value: `${QUEUED_TEXT} edited` } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(useChatStore.getState().queuedMessages).toEqual([
      expect.objectContaining({ text: `${QUEUED_TEXT} edited`, conversationId: CONV }),
    ]);
  });

  it.each([true, false])(
    "recalls the remaining queue after clearing the first recall (history present: %s)",
    (withHistory) => {
      useChatStore.setState({
        queuedMessages: [
          { queueId: "q_first", text: "First follow-up", conversationId: CONV },
          { queueId: "q_last", text: QUEUED_TEXT, conversationId: CONV },
          { queueId: "q_other", text: "Other chat", conversationId: "conv_other" },
        ],
      });
      if (withHistory) {
        appendPromptHistoryEntry("First follow-up", CONV);
        appendPromptHistoryEntry(QUEUED_TEXT, CONV);
      }
      renderWithTooltips(<Composer {...composerProps()} />);

      fireEvent.keyDown(textarea(), { key: "ArrowUp" });
      expect(textarea()).toHaveValue(QUEUED_TEXT);
      expect(useChatStore.getState().queuedMessages.map((message) => message.queueId)).toEqual([
        "q_first",
        "q_other",
      ]);

      fireEvent.change(textarea(), { target: { value: "" } });
      fireEvent.keyDown(textarea(), { key: "ArrowUp" });

      expect(textarea()).toHaveValue("First follow-up");
      expect(useChatStore.getState().queuedMessages.map((message) => message.queueId)).toEqual([
        "q_other",
      ]);
    },
  );

  it("adopts the queued row's attachments so re-sending keeps them", async () => {
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    useChatStore.setState({
      queuedMessages: [{ queueId: "q_1", text: QUEUED_TEXT, conversationId: CONV, files: [file] }],
    });
    appendPromptHistoryEntry(QUEUED_TEXT, CONV);
    renderWithTooltips(<Composer {...composerProps()} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    await waitFor(() => expect(textarea().value).toBe(QUEUED_TEXT));
    expect(screen.getByText("notes.txt")).toBeTruthy();
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
  });

  it.each([
    ["", "screenshot.png"],
    ["Look at this", "screenshot.png"],
    ["", ""],
    ["Look at this", ""],
  ])("preserves queued text %j and screenshot %j when edited and re-sent", (text, name) => {
    const file = new File([new Uint8Array(10)], name, { type: "image/png" });
    const displayName = name || "image.png";
    useChatStore.setState({
      queuedMessages: [{ queueId: "q_image", text, conversationId: CONV, files: [file] }],
    });
    const onSend = vi.fn(useChatStore.getState().enqueueMessage);
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    const strip = screen.getByTestId("composer-queued-strip");
    expect(strip).toHaveTextContent(displayName);
    if (text) expect(strip).toHaveTextContent(text);
    expect(useChatStore.getState().queuedMessages[0]?.text).toBe(text);

    fireEvent.click(screen.getByRole("button", { name: "Edit queued message" }));

    expect(textarea()).toHaveValue(text);
    expect(screen.getByAltText(displayName)).toBeInTheDocument();
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
    fireEvent.keyDown(textarea(), { key: "Enter" });

    expect(onSend).toHaveBeenCalledWith(text, [file]);
    expect(onSend.mock.calls[0]?.[1]?.[0]).toBe(file);
    expect(file.name).toBe(name);
    expect(useChatStore.getState().queuedMessages).toEqual([
      expect.objectContaining({ text, files: [file], conversationId: CONV }),
    ]);
    const requeuedStrip = screen.getByTestId("composer-queued-strip");
    expect(requeuedStrip).toHaveTextContent(displayName);
    if (text) expect(requeuedStrip).toHaveTextContent(text);
  });

  it("preserves a queued quoted reply and its attachments on re-send", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "", text: "Quoted answer" }],
      text: "Follow-up",
    };
    const text = serializeReplyDraft(replyDraft);
    const file = new File(["notes"], "notes.txt", { type: "text/plain" });
    useChatStore.setState({
      queuedMessages: [
        { queueId: "q_reply", text, replyDraft, files: [file], conversationId: CONV },
      ],
    });
    appendPromptHistoryEntry(text, CONV, replyDraft);
    const props = composerProps();
    renderWithTooltips(<Composer {...props} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Quoted answer");
    expect(textarea()).toHaveValue("Follow-up");
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith(text, [file], replyDraft);
  });

  it.each([false, true])("recalls queued path mentions with quotes: %s", (quoted) => {
    useChatStore.setState({ queuedMessages: [], sessionHarness: "codex-native" });
    const props = composerProps({ onSend: useChatStore.getState().enqueueMessage });
    const ref = createRef<ComponentRef<typeof Composer>>();
    renderWithTooltips(<Composer {...props} ref={ref} />);
    act(() =>
      useChatStore.setState({
        pendingComposerAttachments: [{ path: "src/example.ts", isDir: false }],
      }),
    );
    if (quoted) act(() => ref.current?.appendReplyQuote("Quoted answer"));
    fireEvent.change(textarea(), { target: { value: QUEUED_TEXT } });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    const original = useChatStore.getState().queuedMessages[0]!;
    expect(original.text).toContain("[Attached file: src/example.ts]");

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
    expect(screen.queryAllByTestId("composer-reply-quote")).toHaveLength(quoted ? 1 : 0);
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(useChatStore.getState().queuedMessages).toEqual([
      expect.objectContaining({ text: original.text }),
    ]);
    expect(useChatStore.getState().queuedMessages[0]?.replyDraft).toEqual(original.replyDraft);
  });

  it("restores the queued quote metadata even when history has only plain text", () => {
    const replyDraft: StoredReplyDraft = {
      version: 1,
      quotes: [{ before: "", text: "Quoted answer" }],
      text: "Follow-up",
    };
    const text = serializeReplyDraft(replyDraft);
    useChatStore.setState({
      queuedMessages: [{ queueId: "q_reply", text, replyDraft, conversationId: CONV }],
    });
    appendPromptHistoryEntry(text, CONV);
    renderWithTooltips(<Composer {...composerProps()} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    expect(textarea()).toHaveValue("Follow-up");
    expect(screen.getByTestId("composer-reply-quote")).toHaveTextContent("Quoted answer");
    expect(useChatStore.getState().queuedMessages).toHaveLength(0);
  });

  it("keeps an attachment-only draft when browsing history", () => {
    const file = new File(["draft"], "draft.txt", { type: "text/plain" });
    setSessionDraft(CONV, { text: "", files: [file] });
    appendPromptHistoryEntry(QUEUED_TEXT, CONV);
    const props = composerProps();
    renderWithTooltips(<Composer {...props} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    expect(textarea()).toHaveValue(QUEUED_TEXT);
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith(QUEUED_TEXT, [file]);
  });

  it("browsing history over a non-empty draft leaves the queued row alone", async () => {
    appendPromptHistoryEntry(QUEUED_TEXT, CONV);
    renderWithTooltips(<Composer {...composerProps()} />);

    const ta = textarea();
    fireEvent.change(ta, { target: { value: "half-typed draft" } });
    ta.setSelectionRange(0, 0);
    fireEvent.keyDown(ta, { key: "ArrowUp" });

    await waitFor(() => expect(textarea().value).toBe(QUEUED_TEXT));
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);
  });

  it("leaves another conversation's identically-worded queued row alone", async () => {
    useChatStore.setState({
      queuedMessages: [{ queueId: "q_other", text: QUEUED_TEXT, conversationId: "conv_other" }],
    });
    appendPromptHistoryEntry(QUEUED_TEXT, CONV);
    renderWithTooltips(<Composer {...composerProps()} />);

    fireEvent.keyDown(textarea(), { key: "ArrowUp" });

    await waitFor(() => expect(textarea().value).toBe(QUEUED_TEXT));
    expect(useChatStore.getState().queuedMessages).toHaveLength(1);
  });
});

describe("Composer config gear", () => {
  beforeEach(() => {
    setComposerState({
      conversationId: "conv_test",
      skills: [],
      sessionModelOverride: null,
      llmModel: null,
      nativeVendorOwnsModel: false,
      sessionReasoningEffort: null,
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
    useChatStore.setState({ sessionReasoningEffort: "high", sessionHarness: "claude-sdk" });
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
    useChatStore.setState({ sessionReasoningEffort: "high", sessionHarness: "claude-sdk" });
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
    useChatStore.setState({ costControlModeOverride: "on", sessionReasoningEffort: "high" });
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

  it.each([
    { costRoutingEligible: false, showModels: true },
    { costRoutingEligible: true, showModels: false },
    { costRoutingEligible: false, showModels: false },
  ])(
    "hides unavailable Smart Routing and its separator ($costRoutingEligible, $showModels)",
    async ({ costRoutingEligible, showModels }) => {
      renderWithTooltips(
        <Composer
          {...composerProps({ costRoutingEligible, showModels, modelPickerKind: "claude" })}
        />,
      );
      openSessionConfig();
      if (showModels) await openSessionModels();
      expect(screen.queryByRole("menuitem", { name: "Smart Routing" })).toBeNull();
      expect(screen.queryByRole("separator")).toBeNull();
      expect(screen.getByTestId("composer-agent-effort-select")).toBeVisible();
    },
  );

  it.each(["off", "on"] as const)(
    "shows available Smart Routing only in the Model submenu when routing is %s",
    async (costControlModeOverride) => {
      useChatStore.setState({ costControlModeOverride });
      renderWithTooltips(
        <Composer
          {...composerProps({
            costRoutingEligible: true,
            showModels: true,
            modelPickerKind: "claude",
          })}
        />,
      );
      openSessionConfig();
      const rootMenu = within(screen.getByTestId("composer-agent-menu"));
      expect(rootMenu.queryByRole("menuitem", { name: "Smart Routing" })).toBeNull();
      expect(rootMenu.queryByRole("separator")).toBeNull();
      await openSessionModels();
      const menu = within(screen.getByTestId("composer-agent-config-menu"));
      expect(menu.getByRole("menuitem", { name: "Smart Routing" })).not.toHaveAttribute(
        "data-disabled",
      );
      expect(menu.getAllByRole("separator")).toHaveLength(1);
      expect(useChatStore.getState().costControlModeOverride).toBe(costControlModeOverride);
      if (costControlModeOverride === "on") {
        expect(screen.getByTestId("composer-agent-effort-select")).toHaveAttribute("data-disabled");
        expect(screen.getByTestId("composer-agent-effort-select")).toHaveTextContent("Automatic");
      }
    },
  );

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
    expect(menu.lastElementChild).toBe(screen.getByTestId("composer-agent-effort-select"));
    expect(within(menu).queryByRole("separator")).toBeNull();
  });

  it("switches between Model and Effort flyouts on hover and returns to typing on outside click", async () => {
    const user = userEvent.setup();
    renderWithTooltips(
      <Composer {...composerProps({ showModels: true, modelPickerKind: "claude" })} />,
    );
    await user.click(screen.getByTestId("composer-config-gear"));
    await user.hover(screen.getByTestId("composer-agent-edit"));
    expect(await screen.findByTestId("composer-agent-models")).toBeVisible();
    await user.hover(screen.getByTestId("composer-agent-effort-select"));
    expect(await screen.findByTestId("composer-agent-efforts")).toBeVisible();
    expect(screen.queryByTestId("composer-agent-models")).toBeNull();
    await user.click(textarea());
    expect(screen.queryByTestId("composer-agent-menu")).toBeNull();
    expect(textarea()).toHaveFocus();
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
      expect(within(menu).getByTestId("composer-agent-models")).toBeVisible();
      expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
      expect(screen.getAllByRole("menu")).toHaveLength(1);
      fireEvent.click(screen.getByTestId("composer-agent-config-back"));
      expect(screen.queryByTestId("composer-agent-models")).toBeNull();
      fireEvent.click(screen.getByTestId("composer-agent-effort-select"));
      expect(within(menu).getByTestId("composer-agent-efforts")).toBeVisible();
      expect(screen.getAllByRole("menu")).toHaveLength(1);
      fireEvent.click(screen.getByTestId("composer-agent-config-back"));
      expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
      expect(screen.getByTestId("composer-agent-edit")).toBeVisible();
      expect(screen.getByTestId("composer-agent-effort-select")).toBeVisible();
    } finally {
      window.matchMedia = originalMatchMedia;
    }
  });

  it("offers inline effort choices without an Advanced entry in the session picker", async () => {
    useChatStore.setState({
      sessionReasoningEffort: "xhigh",
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
    fireEvent.keyDown(screen.getByTestId("composer-agent-effort-select"), { key: "ArrowRight" });
    expect(screen.queryByTestId("composer-agent-models")).toBeNull();
    expect(screen.queryByRole("separator")).toBeNull();
    expect(await screen.findByTestId("composer-agent-effort-xhigh")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(screen.getByTestId("composer-agent-effort-xhigh")).toHaveTextContent("xHigh");
    expect(screen.queryByText("Advanced settings…")).toBeNull();
  });

  it.each([null, "primary"])(
    "explains an empty native catalog with current model %s and updates when choices arrive",
    async (currentModel) => {
      useChatStore.setState({ llmModel: currentModel });
      const props = composerProps({
        showEffort: false,
        showModels: true,
        modelPickerKind: "codex",
        costRoutingEligible: true,
        codexModelOptions: [],
      });
      const { rerender } = render(<Composer {...props} />, { wrapper: TooltipProvider });
      await openSessionModels();
      const models = screen.getByTestId("composer-agent-models");
      expect(within(models).getByRole("status")).toHaveTextContent(
        "No usable models are available for this session.",
      );
      expect(screen.getByRole("menuitem", { name: "Smart Routing" })).toBeVisible();
      if (currentModel) {
        expect(within(models).getByRole("menuitemcheckbox")).toHaveAttribute(
          "aria-disabled",
          "true",
        );
      } else {
        expect(within(models).queryByRole("menuitemcheckbox")).toBeNull();
      }

      rerender(
        <Composer {...props} codexModelOptions={[{ id: "primary", displayName: "Primary" }]} />,
      );
      expect(within(models).queryByRole("status")).toBeNull();
      expect(within(models).getByRole("menuitemcheckbox", { name: "Primary" })).toBeVisible();
    },
  );

  it("offers only explicit models when Kiro marks no catalog row as default", async () => {
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
    expect(screen.queryByRole("menuitemcheckbox", { name: "Default" })).toBeNull();
    expect(screen.getByRole("menuitemcheckbox", { name: "Automatic" })).toBeVisible();
    expect(screen.getByRole("menuitemcheckbox", { name: "Latest" })).toBeVisible();
  });

  it.each(["claude", "codex", "cursor", "kiro", "pi", "devin"] as const)(
    "selects the default-marked %s row as an explicit model",
    async (modelPickerKind) => {
      const options = [
        { id: "primary", displayName: "Primary", isDefault: true },
        { id: "alternate", displayName: "Alternate" },
      ];
      const setModel = vi.fn().mockResolvedValue(undefined);
      useChatStore.setState({ setModel, codexModelOptions: options });
      renderWithTooltips(
        <Composer
          {...composerProps({
            showEffort: false,
            showModels: true,
            modelPickerKind,
            codexModelOptions: options,
          })}
        />,
      );

      await openSessionModels();
      // A catalog default alone is not evidence of the session's current model.
      expect(screen.getByRole("menuitemcheckbox", { name: "Primary" })).toHaveAttribute(
        "aria-checked",
        "false",
      );
      expect(screen.queryByRole("menuitemcheckbox", { name: "Default" })).toBeNull();
      fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Alternate" }));
      await waitFor(() => expect(setModel).toHaveBeenCalledWith("alternate", expect.anything()));
      act(() =>
        useChatStore.setState({ sessionModelOverride: "alternate", llmModel: "alternate" }),
      );

      await openSessionModels();
      fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Primary" }));
      await waitFor(() =>
        expect(setModel).toHaveBeenLastCalledWith("primary", {
          expectConfirmation: modelPickerKind === "claude" || modelPickerKind === "codex",
        }),
      );
      expect(setModel).toHaveBeenCalledTimes(2);
    },
  );

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
      sessionReasoningEffort: "high",
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
    const effortRow = screen.getByTestId("composer-agent-effort-select");
    expect(effortRow).toHaveAttribute("data-disabled");
    fireEvent.click(effortRow);
    expect(screen.queryByTestId("composer-agent-efforts")).toBeNull();
    expect(setModel).toHaveBeenCalledTimes(1);
    expect(setEffort).not.toHaveBeenCalled();

    // Model fires first and effort waits for its promise to resolve.
    await waitFor(() =>
      expect(setModel).toHaveBeenCalledWith("sonnet", { expectConfirmation: true }),
    );
    expect(setEffort).not.toHaveBeenCalled();
    resolveModel();
    await waitFor(() =>
      expect(screen.getByTestId("composer-agent-effort-select")).not.toHaveAttribute(
        "data-disabled",
      ),
    );
    await openSessionEfforts();
    fireEvent.click(screen.getByTestId("composer-agent-effort-low"));
    await waitFor(() => expect(setEffort).toHaveBeenCalledWith("low"));
    expect(screen.getByTestId("composer-agent-menu")).toBeInTheDocument();
    expect(calls).toEqual(["model", "effort"]);
  });

  it("shows a titled actionable tooltip when a session config update fails", async () => {
    const setModel = vi.fn().mockRejectedValue(new Error("Host stopped responding"));
    const options = [
      { id: "opus", model: "opus", displayName: "Opus" },
      { id: "sonnet", model: "sonnet", displayName: "Sonnet" },
    ] as never;
    useChatStore.setState({ setModel, codexModelOptions: options });
    renderWithTooltips(
      <Composer
        {...composerProps({
          showModels: true,
          modelPickerKind: "claude",
          codexModelOptions: options,
        })}
      />,
    );

    await openSessionModels();
    fireEvent.click(screen.getByTestId("composer-agent-model-sonnet"));
    const error = await screen.findByTestId("composer-config-error");
    fireEvent.focus(error);
    const tooltip = await screen.findByTestId("composer-config-error-tooltip");
    expect(tooltip).toHaveTextContent("Couldn’t update configuration");
    expect(tooltip).toHaveTextContent("Host stopped responding");
    expect(tooltip).toHaveTextContent("Try again");
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
      sessionReasoningEffort: "ultra",
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

    await openSessionEfforts();
    // Sol starts on ultra.
    expect(screen.getByTestId("composer-agent-effort-ultra")).toHaveAttribute(
      "aria-checked",
      "true",
    );

    // Switch to Luna, whose ceiling is "max".
    await openSessionModels();
    fireEvent.click(
      document.querySelector('[data-testid="composer-agent-model-gpt-5.6-luna"]') as Element,
    );

    // The picked ultra is dropped (back to Default) and no longer offered,
    // while Luna's own max stays.
    await waitFor(() => expect(useChatStore.getState().setEffort).toHaveBeenCalledWith(null));
    act(() => useChatStore.setState({ llmModel: "gpt-5.6-luna", sessionReasoningEffort: null }));
    await openSessionEfforts();
    expect(screen.queryByTestId("composer-agent-effort-default")).toBeNull();
    expect(document.querySelector('[data-testid="composer-agent-effort-ultra"]')).toBeNull();
    expect(document.querySelector('[data-testid="composer-agent-effort-max"]')).not.toBeNull();
  });

  it("re-pins the model when turning Smart Routing off, even if the shown model is unchanged", async () => {
    // Routing-on clears the applied override but keeps the cross-session sticky
    // (sessionModelOverride), so the modal shows that model as "resolved". Turning
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
      // Routing on with no applied override.
      costControlModeOverride: "on",
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

  it("sends directly for a /side command even while busy or with a queued message", () => {
    // A codex /side forks its own side chat and is non-interrupting — it must
    // POST now while the parent turn runs, bypassing both the busy gate and the
    // main-thread ordering guard.
    expect(shouldQueueSend("conv_a", "streaming", "running", [], false, true)).toBe(false);
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_a")], false, true)).toBe(false);
  });
});

const skillsFixture = create<{
  skills: SkillSummary[];
  skillsStatus: SkillsStatus | null;
  refetch: ReturnType<typeof vi.fn>;
}>(() => ({ skills: [], skillsStatus: null, refetch: vi.fn() }));

vi.mock("@/hooks/useSkills", () => ({
  useSkills: ({ starting }: { starting: boolean }) => {
    const state = skillsFixture();
    return {
      ...state,
      skillsStatus:
        state.skillsStatus === "unavailable" && starting ? "loading" : state.skillsStatus,
    };
  },
}));

beforeEach(() => skillsFixture.setState({ skills: [], skillsStatus: null, refetch: vi.fn() }));

function setComposerState(
  patch: Partial<ChatState> & { skills?: SkillSummary[]; skillsStatus?: SkillsStatus | null },
) {
  const { skills, skillsStatus, ...chat } = patch;
  useChatStore.setState(chat);
  skillsFixture.setState({
    ...(skills === undefined ? {} : { skills }),
    ...(skillsStatus === undefined ? {} : { skillsStatus }),
  });
}

describe("saved sandbox inference policy", () => {
  let previous: ChatState;
  beforeEach(() => {
    previous = useChatStore.getState();
    useChatStore.setState({
      conversationId: "conv_policy",
      sessionHarness: "claude-sdk",
      sessionModelOverride: null,
      sessionModelSeeded: false,
      llmModel: "private/default",
      costControlModeOverride: null,
      pendingModelChange: null,
      setModel: vi.fn().mockResolvedValue(undefined),
    });
  });
  afterEach(() => {
    cleanup();
    useChatStore.setState(previous, true);
  });

  it.each(["private/default", "private/fast", null])(
    "offers the saved shortlist with the known model %s checked",
    async (llmModel) => {
      useChatStore.setState({ llmModel });
      renderWithTooltips(
        <Composer
          {...composerProps({
            showModels: true,
            showEffort: false,
            modelPickerKind: "configured",
            inferenceConfigured: true,
            codexModelOptions: [
              { id: "private/default", displayName: "Primary", isDefault: true },
              { id: "private/fast", displayName: "Fast" },
            ],
          })}
        />,
      );
      await openSessionModels();
      expect(screen.queryByTestId("composer-agent-model-default")).toBeNull();
      expect(screen.getByRole("menuitemcheckbox", { name: "Primary" })).toHaveAttribute(
        "aria-checked",
        String(llmModel === "private/default"),
      );
      expect(screen.getByRole("menuitemcheckbox", { name: "Fast" })).toHaveAttribute(
        "aria-checked",
        String(llmModel === "private/fast"),
      );
      fireEvent.click(screen.getByRole("menuitemcheckbox", { name: "Fast" }));
      await waitFor(() =>
        expect(useChatStore.getState().setModel).toHaveBeenCalledWith("private/fast", {
          expectConfirmation: false,
        }),
      );
    },
  );

  it.each([
    { error: null, options: [] },
    { error: "The gateway could not be reached.", options: [] },
    {
      error: "The gateway could not be reached.",
      options: [{ id: "private/default", displayName: "Primary", isDefault: true }],
    },
  ])(
    "disables reset when the saved catalog is unavailable ($error, $options)",
    async ({ error, options }) => {
      renderWithTooltips(
        <Composer
          {...composerProps({
            showModels: true,
            showEffort: false,
            modelPickerKind: "configured",
            inferenceConfigured: true,
            inferenceError: error,
            codexModelOptions: options,
          })}
        />,
      );
      await openSessionModels();
      const models = within(screen.getByTestId("composer-agent-models"));
      expect(models.getByRole("status")).toHaveTextContent(
        error ?? "No usable models are available for this session.",
      );
      expect(screen.queryByRole("menuitem", { name: "Use default model" })).toBeNull();
      expect(screen.queryByRole("menuitemcheckbox", { name: "Default" })).toBeNull();
      for (const choice of screen.getAllByRole("menuitemcheckbox")) {
        expect(choice).toHaveAttribute("aria-disabled", "true");
        fireEvent.click(choice);
      }
      expect(useChatStore.getState().setModel).not.toHaveBeenCalled();
    },
  );
});
