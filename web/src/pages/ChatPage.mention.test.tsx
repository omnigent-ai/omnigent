import type * as UseWorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import type * as UseSessionModule from "@/hooks/useSession";
import type * as UseHostsModule from "@/hooks/useHosts";
import type * as RunnerHealthProviderModule from "@/hooks/RunnerHealthProvider";
import type * as AgentLabelsModule from "@/lib/agentLabels";
import type * as UseChildSessionsModule from "@/hooks/useChildSessions";
import type * as UseHarnessInventoryModule from "@/hooks/useHarnessInventory";
import type * as UseSessionLiveMcpServersModule from "@/hooks/useSessionLiveMcpServers";
import type * as UseAgentsModule from "@/hooks/useAgents";
import type * as UseConversationsModule from "@/hooks/useConversations";
import type * as UseSidebarDataModule from "@/hooks/useSidebarData";

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkspaceFile } from "@/hooks/useWorkspaceChangedFiles";

// Drill-down "@"-mention browses one directory at a time, so the test stubs
// both sources: the root listing (useWorkspaceAllFiles) and the per-directory
// listing (useWorkspaceDirectory). Root holds a "src" folder + a file; "src"
// holds two nested files reachable only by opening it.
//
// Mock returns read from mutable hoisted state so a single test can flip a
// listing to "still loading" or swap in a larger set, without re-mocking the
// module. ``resetWorkspaceMock`` (called in beforeEach) restores the defaults.
const ws = vi.hoisted(() => ({
  rootEntries: [] as unknown[],
  srcEntries: [] as unknown[],
  rootLoading: false,
  dirLoading: false,
  searchFiles: [] as unknown[],
}));
// Harness ids the server declares ``workspace_file_mentions`` for.
const declaredMentions = vi.hoisted(() => ({ harnesses: new Set<string>() }));
const ROOT_ENTRIES: WorkspaceFile[] = [
  { path: "src", name: "src", type: "directory", bytes: null, modified_at: null },
  { path: "readme.md", name: "readme.md", type: "file", bytes: 10, modified_at: null },
];
const SRC_ENTRIES: WorkspaceFile[] = [
  { path: "src/server.ts", name: "server.ts", type: "file", bytes: 10, modified_at: null },
  { path: "src/client.ts", name: "client.ts", type: "file", bytes: 10, modified_at: null },
];
function resetWorkspaceMock() {
  ws.rootEntries = ROOT_ENTRIES;
  ws.srcEntries = SRC_ENTRIES;
  ws.rootLoading = false;
  ws.dirLoading = false;
  ws.searchFiles = [];
  sessions.loaded = [];
  sessions.children = [];
  sessions.searchHits = [];
  sessions.searchedQueries = [];
}

// Session rows for the "@" menu: sidebar-loaded rows, the current session's
// subagents, and server title-search hits (recorded so tests can assert when
// the search is skipped).
const sessions = vi.hoisted(() => ({
  loaded: [] as unknown[],
  children: [] as unknown[],
  searchHits: [] as unknown[],
  searchedQueries: [] as string[],
}));
vi.mock("@/hooks/useSidebarData", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSidebarDataModule>()),
  useOptionalLoadedConversations: () => ({
    pages: [{ data: sessions.loaded }],
    pageParams: [undefined],
  }),
}));
vi.mock("@/hooks/useConversations", async (importOriginal) => ({
  ...(await importOriginal<typeof UseConversationsModule>()),
  useSessionSearch: (query: string, _scope: string, options: { enabled?: boolean }) => {
    const active = (options.enabled ?? true) && query !== "";
    if (active) sessions.searchedQueries.push(query);
    return { data: active ? { pages: [{ data: sessions.searchHits }] } : undefined };
  },
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof UseWorkspaceChangedFilesModule>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({
      data: { available: true, data: ws.rootEntries },
      isLoading: ws.rootLoading,
    }),
    useWorkspaceDirectory: (_conv: string | undefined, dirPath: string | null) => ({
      data: dirPath === "src" ? ws.srcEntries : undefined,
      isLoading: ws.dirLoading,
    }),
    useWorkspaceFileSearch: (_conv: string | undefined, query: string) => ({
      data: query ? { files: ws.searchFiles, truncated: false } : undefined,
    }),
  };
});

// ComposerStatusLine's PR link reads GitHub info via a TanStack query; stub it
// (default: no PR) so bare Composer renders don't need a QueryClientProvider.
vi.mock("@/hooks/usePullRequests", () => ({
  usePullRequestInfo: () => ({ data: undefined }),
}));
vi.mock("@/hooks/useComposerGitStatus", () => ({
  useComposerGitStatus: () => ({ branchState: "unknown", prCount: 0 }),
}));
vi.mock("@/hooks/useSkills", () => ({
  useSkills: () => ({ skills: [], skillsStatus: "ready", refetch: vi.fn() }),
}));
vi.mock("@/hooks/useHarnessInventory", async (importOriginal) => ({
  ...(await importOriginal<typeof UseHarnessInventoryModule>()),
  useHostMcpServers: () => ({ data: [], isLoading: false, isError: false }),
}));
vi.mock("@/hooks/useSessionLiveMcpServers", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionLiveMcpServersModule>()),
  useSessionLiveMcpServers: () => ({ data: undefined, isError: false }),
  useReconnectLiveMcpServer: () => ({
    reconnect: () => {},
    reconnectingName: null,
    error: null,
    clearError: () => {},
  }),
}));
// /mcp reads the session's agent via a TanStack query; stub it (no agent loaded)
// so bare Composer renders don't need a QueryClientProvider.
vi.mock("@/hooks/useAgents", async (importOriginal) => ({
  ...(await importOriginal<typeof UseAgentsModule>()),
  useSessionAgent: () => ({ data: undefined, isError: false }),
}));
vi.mock("@/hooks/useChildSessions", async (importOriginal) => ({
  ...(await importOriginal<typeof UseChildSessionsModule>()),
  useChildSessions: () => ({ children: sessions.children }),
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
  useFileMentionHarnesses: () => declaredMentions.harnesses,
}));

import { Composer, detectMentionAt, mentionMarkerFor } from "./ChatPage";
import { SESSION_MENTION_CAP, SESSION_MENTION_HINT } from "@/lib/composerMentions";

function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onNewSession: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    agentsLoading: false,
    selectedAgentId: null,
    onSelectAgent: vi.fn(),
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

function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

/** Type `text` into the composer with the caret at the end. */
function type(text: string) {
  fireEvent.change(textarea(), { target: { value: text, selectionStart: text.length } });
}

/** Append `text` to the draft with the caret at the end. */
function append(text: string) {
  type(textarea().value + text);
}

/** Tokens the composer mirror currently paints as pills. */
function tokens() {
  return screen.queryAllByTestId("composer-mention-token").map((token) => token.textContent);
}

function renderWithTooltips(ui: ReactElement) {
  return render(<TooltipProvider>{ui}</TooltipProvider>);
}

describe("detectMentionAt", () => {
  it("triggers on an @ at line start", () => {
    expect(detectMentionAt("@ser", 4)).toEqual({ query: "ser", start: 0, end: 4 });
  });

  it("triggers on an @ after whitespace, capturing only the token", () => {
    expect(detectMentionAt("look @src/cli", 13)).toEqual({ query: "src/cli", start: 5, end: 13 });
  });

  it("does NOT trigger when the @ is glued to a preceding word (e.g. an email)", () => {
    // WHY: "me@host" is an email, not a mention — gluing to a word must not
    // open the file browser mid-typing.
    expect(detectMentionAt("me@host", 7)).toBeNull();
  });

  it("closes once the token is finished by a space", () => {
    // WHY: a trailing space means the user finished the token; the menu must
    // close so the next keystroke isn't captured as a mention.
    expect(detectMentionAt("@server.ts ", 11)).toBeNull();
  });

  it("reads the token at the caret, ignoring text after it (mid-token edit)", () => {
    // WHY: the user can move the caret back into a token to refine it; only the
    // text up to the caret defines the active query, never the trailing text.
    expect(detectMentionAt("@server.ts and more", 4)).toEqual({
      query: "ser",
      start: 0,
      end: 4,
    });
  });

  it("triggers after a newline (the @ sits at the start of a later line)", () => {
    // WHY: ^ anchors to the start of the sliced string, but a preceding newline
    // counts as \s, so an "@" beginning a second line still triggers.
    expect(detectMentionAt("hello\n@src", 10)).toEqual({ query: "src", start: 6, end: 10 });
  });

  it("triggers on a bare '@/' (root-relative path opener)", () => {
    expect(detectMentionAt("@/", 2)).toEqual({ query: "/", start: 0, end: 2 });
  });

  it("captures only the LAST @ when several appear", () => {
    // WHY: a second "@" after whitespace starts a fresh token; an earlier one
    // in the line must not bleed into the query.
    expect(detectMentionAt("@a @b", 5)).toEqual({ query: "b", start: 3, end: 5 });
  });
});

describe("mentionMarkerFor", () => {
  // Wording is load-bearing: codex echoes its own "[Attached file: …]" form
  // back in the mirrored transcript, while claude/pi/cursor use "[Attached: …]".
  it("uses the 'Attached file:' wording for codex", () => {
    expect(mentionMarkerFor("codex-native", "src/a.ts")).toBe("[Attached file: src/a.ts]");
  });

  it("uses the plain 'Attached:' wording for claude/pi/cursor", () => {
    expect(mentionMarkerFor("claude-native", "src/a.ts")).toBe("[Attached: src/a.ts]");
    expect(mentionMarkerFor("pi-native", "src/a.ts")).toBe("[Attached: src/a.ts]");
    expect(mentionMarkerFor("cursor-native", "src/a.ts")).toBe("[Attached: src/a.ts]");
  });

  it("resolves an aliased harness through the registry, not a raw string compare", () => {
    // WHY (H6): "native-pi" is a reversed spelling the registry folds to
    // "pi-native"; routing through nativeCodingAgentForHarness means it still
    // produces the plain wording rather than being treated as unknown.
    // (The codex reversed form "native-codex" is NOT in the frontend alias map
    // — aligning that map with the server is M10, out of this staged scope.)
    expect(mentionMarkerFor("native-pi", "a.ts")).toBe("[Attached: a.ts]");
  });

  it("falls back to the plain wording for a null / unknown harness", () => {
    // WHY (L11): the drain effect can apply a queued chip before sessionHarness
    // resolves, so mentionMarkerFor(null, …) must not throw and must pick a
    // sane default.
    expect(mentionMarkerFor(null, "a.ts")).toBe("[Attached: a.ts]");
    expect(mentionMarkerFor("some-sdk-harness", "a.ts")).toBe("[Attached: a.ts]");
  });
});

describe("Composer @-file-mention browser (native sessions)", () => {
  // Each test gets a fresh conversation id and a cleared draft store: the
  // module-scoped ``sessionDrafts`` map (persisted to localStorage) is keyed by
  // conversationId, so reusing one id leaks an unsent "@…" draft into the next
  // test's freshly-mounted composer, which then re-derives the wrong mention
  // state (L12). Distinct ids keep every test self-contained.
  let convSeq = 0;
  beforeEach(() => {
    resetWorkspaceMock();
    declaredMentions.harnesses = new Set();
    localStorage.clear();
    useChatStore.setState({
      conversationId: `conv_test_${++convSeq}`,
      sessionHarness: "claude-native",
      pendingComposerAttachments: [],
    });
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("lists the root directory's folders and files when '@' is typed", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
    expect(screen.getByTitle("Attach readme.md")).toBeInTheDocument();
  });

  it("gives Shift+Tab to the open menu before the permission-mode shortcut", async () => {
    const { setClaudePermissionMode } = useChatStore.getState();
    // Zustand copies the spy into later states, so put the real setter back.
    onTestFinished(() =>
      useChatStore.setState({ setClaudePermissionMode, claudePermissionMode: "" }),
    );
    useChatStore.setState({ claudePermissionMode: "default" });
    const setMode = vi
      .spyOn(useChatStore.getState(), "setClaudePermissionMode")
      .mockResolvedValue(undefined);
    renderWithTooltips(<Composer {...composerProps({ showClaudePermissionMode: true })} />);
    type("@");
    expect(screen.getByTitle("Attach readme.md")).toBeInTheDocument();

    fireEvent.keyDown(textarea(), { key: "Tab", shiftKey: true });
    expect(textarea().value).toBe("@src/ ");
    expect(setMode).not.toHaveBeenCalled();

    // Attaching a folder closes the menu, so the next press cycles the mode.
    fireEvent.keyDown(textarea(), { key: "Tab", shiftKey: true });
    await waitFor(() => expect(setMode).toHaveBeenCalledWith("auto"));
  });

  it("opens a folder to reveal nested files, then delivers the chosen file", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    type("@src");
    // Nested files are NOT visible until the folder is opened (drill-down).
    expect(screen.queryByTitle("Attach server.ts")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Open src"));

    // Now inside src: nested files appear.
    fireEvent.click(screen.getByTitle("Attach server.ts"));
    // The token lands in the sentence and the mirror paints it as a pill.
    expect(textarea().value).toBe("@src/server.ts ");
    expect(tokens()).toEqual(["@src/server.ts"]);

    append("explain this");
    fireEvent.submit(textarea().closest("form")!);

    // The WHY: the agent reads the on-disk nested file from this exact marker,
    // in place in the sentence. No File is uploaded (second arg undefined).
    expect(onSend).toHaveBeenCalledWith("[Attached: src/server.ts] explain this", undefined);
  });

  it("attaches a whole folder as a single trailing-slash marker", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    type("@src");
    fireEvent.click(screen.getByLabelText("Attach whole folder src"));
    expect(tokens()).toEqual(["@src/"]);

    append("review the module");
    fireEvent.submit(textarea().closest("form")!);

    // A folder is delivered with a trailing "/" so the agent opens the dir.
    expect(onSend).toHaveBeenCalledWith("[Attached: src/] review the module", undefined);
  });

  // Wording per harness is covered by mentionMarkerFor above; this checks the
  // composer passes the session's harness through.
  it("delivers the harness-specific marker wording", () => {
    useChatStore.setState({ sessionHarness: "codex-native" });
    const expected = "check [Attached file: src/server.ts] now";
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("check @src");
    fireEvent.click(screen.getByTitle("Open src"));
    fireEvent.click(screen.getByTitle("Attach server.ts"));
    append("now");
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith(expected, undefined);
  });

  it("drains a file-viewer 'Attach to agent' line range into an inline token and marker", () => {
    const onSend = vi.fn();
    // Simulate the file viewer's button having queued a line span.
    useChatStore.setState({
      pendingComposerAttachments: [
        { path: "bob-max-gain/docker-compose.yml", isDir: false, lineRange: { start: 2, end: 9 } },
      ],
    });
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    // The composer drained the queue into a token with the path + lines...
    expect(tokens()).toEqual(["@bob-max-gain/docker-compose.yml:2-9"]);
    // ...and the store queue was cleared so it can't double-apply.
    expect(useChatStore.getState().pendingComposerAttachments).toEqual([]);

    append("explain this service");
    fireEvent.submit(textarea().closest("form")!);

    // Delivered as "path:start-end" inside the marker so the agent reads
    // exactly those lines.
    expect(onSend).toHaveBeenCalledWith(
      "[Attached: bob-max-gain/docker-compose.yml:2-9] explain this service",
      undefined,
    );
  });

  it("does NOT show the mention menu on a non-native harness the server doesn't declare", () => {
    useChatStore.setState({ sessionHarness: "claude-sdk" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@src");
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
  });

  it("delivers mentions on a non-native harness the server declares", () => {
    declaredMentions.harnesses = new Set(["claude-sdk"]);
    useChatStore.setState({ sessionHarness: "claude-sdk" });
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("check @src");
    fireEvent.click(screen.getByTitle("Open src"));
    fireEvent.click(screen.getByTitle("Attach server.ts"));
    append("now");
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith("check [Attached: src/server.ts] now", undefined);
  });

  it("opens the menu on an aliased native harness (native-pi → pi-native)", () => {
    // WHY (H6): the composer's "@" gate and the file viewer's attach gate must
    // agree on "is this native?". Both now resolve through the agent registry,
    // which folds the reversed "native-pi" spelling — a literal string compare
    // would open the viewer's attach button but leave "@" dead here.
    useChatStore.setState({ sessionHarness: "native-pi" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
  });

  // ── Keyboard navigation (H4) ────────────────────────────────────────────
  // The slash menu has keyboard tests; the mention menu's Arrow/Enter/Tab/Esc
  // branches were entirely uncovered, so a regression would pass silently.

  it("ArrowDown moves to the next row and Enter acts on the highlighted one", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    const ta = textarea();
    // Row 0 (src, folders-first) is preselected; move to row 1 (readme.md).
    fireEvent.keyDown(ta, { key: "ArrowDown" });
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(tokens()).toEqual(["@readme.md"]);
  });

  it("ArrowUp from the top row wraps to the last row", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // From row 0, ArrowUp wraps to the last row (readme.md), not -1.
    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(tokens()).toEqual(["@readme.md"]);
  });

  it("Enter on a directory row drills in rather than attaching", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // Row 0 is the "src" folder: Enter must open it (reveal nested files), and
    // must NOT tag it — drilling is navigation, not attachment, so the typed
    // "@src/" stays plain text with no pill.
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(screen.getByTitle("Attach server.ts")).toBeInTheDocument();
    expect(tokens()).toEqual([]);
  });

  it("Tab attaches the highlighted folder as a whole-directory unit", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // Tab on the "src" folder attaches it as a unit (trailing-slash token),
    // distinct from Enter's drill-in.
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(tokens()).toEqual(["@src/"]);
  });

  it("Escape closes the mention menu", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
  });

  it("adds deeper files from the recursive search, deduped and filtered by the live query", async () => {
    ws.searchFiles = [
      { path: "src/server.ts", name: "server.ts", type: "file", bytes: 1, modified_at: null },
      // A stale hit from an earlier, shorter query no longer matches "serv".
      { path: "src/client.ts", name: "client.ts", type: "file", bytes: 1, modified_at: null },
    ];
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@serv");
    // The root listing has no match; the debounced search supplies the nested file.
    await waitFor(() => expect(screen.getByTitle("Attach server.ts")).toBeInTheDocument());
    expect(screen.queryByTitle("Attach client.ts")).not.toBeInTheDocument();
    expect(screen.getAllByTitle("Attach server.ts")).toHaveLength(1);
  });

  it("dedups a file both listed and returned by the recursive search", () => {
    vi.useFakeTimers();
    onTestFinished(() => {
      vi.useRealTimers();
    });
    ws.searchFiles = [
      { path: "readme.md", name: "readme.md", type: "file", bytes: 10, modified_at: null },
      {
        path: "docs/readme-notes.md",
        name: "readme-notes.md",
        type: "file",
        bytes: 1,
        modified_at: null,
      },
    ];
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@readme");
    act(() => vi.advanceTimersByTime(300));
    // The search ran (its deeper hit shows) and the root file appears once.
    expect(screen.getByTitle("Attach readme-notes.md")).toBeInTheDocument();
    expect(screen.getAllByTitle("Attach readme.md")).toHaveLength(1);
  });

  it("gives a file its own token when a tagged session has the same name", () => {
    const onSend = vi.fn();
    sessions.loaded = [sessionRow("s_readme", "readme.md")];
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("compare @readme");
    fireEvent.click(screen.getByTitle("Tag session readme.md"));
    append("with @readme");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    expect(tokens()).toEqual(["@readme.md", "@readme.md (file)"]);
    // Deleting the session token leaves only the file; its longer token must not
    // keep the session tagged (which would still add the session hint).
    type("compare  with @readme.md (file) ");
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith("compare  with [Attached: readme.md]", undefined);
  });

  it("re-adds a file on 'Attach to agent' when only a longer session token still starts with it", () => {
    const onSend = vi.fn();
    sessions.loaded = [sessionRow("s_readme", "readme.md")];
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    // File first, so the session gets the longer, disambiguated token.
    type("compare @readme");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    append("with @readme");
    fireEvent.click(screen.getByTitle("Tag session readme.md"));
    const [fileToken, sessionToken] = tokens();
    expect(fileToken).toBe("@readme.md");
    expect(sessionToken.startsWith("@readme.md ")).toBe(true);
    // Delete the standalone file token, then attach the file again from the viewer.
    type(`compare  with ${sessionToken} `);
    act(() => {
      useChatStore.setState({ pendingComposerAttachments: [{ path: "readme.md", isDir: false }] });
    });
    expect(tokens()).toEqual([sessionToken, "@readme.md"]);
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend.mock.calls[0]![0]).toBe(
      'compare  with [Session: "readme.md" id=s_readme subagent=no] [Attached: readme.md]' +
        `\n\n${SESSION_MENTION_HINT}`,
    );
  });

  it("keeps the highlighted file when session results land above it", async () => {
    sessions.searchHits = [sessionRow("s_readme", "readme cleanup")];
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@readme");
    // Before the debounced search returns, the file is the top, highlighted row.
    expect(screen.getByTestId("file-mention-item-0")).toHaveAttribute("data-active", "true");
    await waitFor(() =>
      expect(screen.getByTitle("Tag session readme cleanup")).toBeInTheDocument(),
    );
    // The session row was inserted above, but the highlight stayed on the file.
    expect(screen.getByTestId("file-mention-item-0")).toHaveAttribute("data-active", "true");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(tokens()).toEqual(["@readme.md"]);
  });

  // ── Token lifecycle + dedup (M8, M2) ─────────────────────────────────────

  it("Backspace at a token's end deletes the whole token and untags it", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("see @");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    // Caret just after the token (the trailing space removed), which reopens the
    // menu for "@readme.md".
    type("see @readme.md");
    expect(screen.getByTitle("Attach readme.md")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Backspace" });
    expect(textarea().value).toBe("see ");
    expect(tokens()).toEqual([]);
    expect(screen.queryByTitle("Attach readme.md")).not.toBeInTheDocument();
    // Enter right away sends the text; it must not re-insert the deleted token.
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("see", undefined);
  });

  it("treats a deleted tag's retyped text as literal text", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("see @");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    type("see @readme.md");
    fireEvent.keyDown(textarea(), { key: "Backspace" });
    expect(tokens()).toEqual([]);
    // Typing the same text again, without picking a suggestion, is not a tag.
    type("Explain the literal text @readme.md here");
    expect(tokens()).toEqual([]);
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith("Explain the literal text @readme.md here", undefined);
  });

  it("keeps a tag while another of its tokens remains", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("see @");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    append("and @readme");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    expect(tokens()).toEqual(["@readme.md", "@readme.md"]);
    // Deleting one occurrence leaves the other tagged.
    type("see and @readme.md ");
    expect(tokens()).toEqual(["@readme.md"]);
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith("see and [Attached: readme.md]", undefined);
  });

  it("dedups re-attaching the same file via '@' to one tag", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("@");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    append("and @");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    // Same path + dir-ness + (no) range → same identity key → one tag, and
    // each occurrence of its token becomes the marker.
    expect(tokens()).toEqual(["@readme.md", "@readme.md"]);
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith(
      "[Attached: readme.md] and [Attached: readme.md]",
      undefined,
    );
  });

  it("keeps two distinct line ranges of the same file as separate tokens", () => {
    // WHY (M2): the dedup key is path + dir-ness + line range, NOT path alone.
    // A path-only key would silently drop the second range — but two ranges of
    // one file are genuinely distinct attachments the agent should receive.
    useChatStore.setState({
      pendingComposerAttachments: [
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
        { path: "a.ts", isDir: false, lineRange: { start: 20, end: 30 } },
      ],
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(tokens()).toEqual(["@a.ts:2-9", "@a.ts:20-30"]);
  });

  it("collapses identical queued attachments to one token on drain", () => {
    // The store dedups on insert, but the drain must also dedup within a batch
    // (and against already-tagged chips) so a duplicated queue can't double up.
    useChatStore.setState({
      pendingComposerAttachments: [
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
      ],
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(textarea().value).toBe("@a.ts:2-9 ");
  });

  // ── Dismissal + loading feedback (M3, M1, M6) ─────────────────────────────

  it("dismisses the menu when the textarea loses focus", () => {
    // WHY (M3): the menu's visibility derives from the caret on change; without
    // a blur dismiss it lingers when the user clicks another field.
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
    fireEvent.blur(textarea());
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
  });

  it("shows a loading row and swallows Enter while the listing is still fetching", () => {
    // WHY (M1+M6): during the cold-boot/drill fetch window the listing is empty
    // so the menu is closed — a stray Enter must not send the half-typed token
    // as a chat message, and the user needs feedback that "@" is alive.
    ws.rootLoading = true;
    ws.rootEntries = [];
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("@comp");
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).not.toHaveBeenCalled();
  });

  it("still sends a settled zero-match token literally (not gated)", () => {
    // WHY (M1 boundary): the gate is for the *loading* window only. A token
    // that simply matches no file ("@nomatch") is the user's literal text and
    // Enter must send it — gating all non-null mentions would trap that.
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("ping @nomatch");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("ping @nomatch", undefined);
  });
});

function sessionRow(id: string, title: string | null, extra: Record<string, unknown> = {}) {
  return { id, object: "conversation", title, created_at: 0, updated_at: 0, labels: {}, ...extra };
}

describe("Composer @-session mentions", () => {
  let convSeq = 0;
  let currentId = "";
  beforeEach(() => {
    resetWorkspaceMock();
    localStorage.clear();
    currentId = `conv_session_${++convSeq}`;
    useChatStore.setState({
      conversationId: currentId,
      sessionHarness: "claude-sdk",
      pendingComposerAttachments: [],
    });
    sessions.loaded = [
      sessionRow(currentId, "This session"),
      sessionRow("s_alpha", "Alpha planning"),
      sessionRow("s_archived", "Alpha archived", { archived: true }),
      sessionRow("s_beta", null),
    ];
    sessions.children = [
      {
        id: "s_child",
        title: "researcher:auth",
        task_summary: null,
        tool: "researcher",
        session_name: "auth",
        current_task_status: null,
        busy: false,
        last_message_preview: null,
        pending_elicitations_count: 0,
      },
    ];
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("collapses sessions into one row on a bare @ and opens the lists from it", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // A bare "@" shows only the Sessions row: no session rows, no files on SDK harnesses.
    expect(screen.getByTestId("session-mention-category-sessions")).toHaveAttribute(
      "data-active",
      "true",
    );
    expect(screen.queryAllByTestId(/^session-mention-item-/)).toHaveLength(0);
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("session-mention-category-sessions"));
    // A Subagents row, then the other sessions; the current session and archived
    // rows are excluded, and untitled rows get a label.
    expect(screen.getByTestId("session-mention-category-subagents")).toBeInTheDocument();
    const rowText = () =>
      screen.getAllByTestId(/^session-mention-item-/).map((row) => row.textContent);
    expect(rowText()).toEqual(["Alpha planning", "Untitled session"]);
    fireEvent.click(screen.getByTestId("session-mention-category-subagents"));
    expect(rowText()).toEqual(["authsubagent"]);
    // The lists are served from cache alone.
    expect(sessions.searchedQueries).toEqual([]);
  });

  it("lists every loaded session when opened but caps typed matches", () => {
    sessions.loaded = Array.from({ length: 12 }, (_, i) => sessionRow(`s_${i}`, `Plan ${i}`));
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    fireEvent.keyDown(textarea(), { key: "Enter" });
    // All twelve loaded sessions, below the Subagents row.
    expect(screen.getAllByTestId(/^session-mention-item-/)).toHaveLength(12);
    type("@plan");
    // Typing in the open list filters it without a cap.
    expect(screen.getAllByTestId(/^session-mention-item-/)).toHaveLength(12);
    fireEvent.keyDown(textarea(), { key: "Escape" });
    // Back at the top level, typed matches are capped inline.
    expect(screen.getAllByTestId(/^session-mention-item-/)).toHaveLength(SESSION_MENTION_CAP);
  });

  it("goes up one level with Back or Escape, then Escape closes the menu", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    fireEvent.click(screen.getByTestId("session-mention-category-sessions"));
    // An open list hides files.
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("session-mention-back"));
    expect(screen.getByTestId("session-mention-category-sessions")).toBeInTheDocument();
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
    // ArrowRight opens the highlighted category: Sessions, then Subagents.
    fireEvent.keyDown(textarea(), { key: "ArrowRight" });
    fireEvent.keyDown(textarea(), { key: "ArrowRight" });
    expect(screen.queryByTestId("session-mention-category-subagents")).not.toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.getByTestId("session-mention-category-subagents")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.getByTestId("session-mention-category-sessions")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.queryByTestId("file-mention-menu")).not.toBeInTheDocument();
  });

  it("delivers tagged sessions as inline markers plus one tool hint", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("Ask @");
    // Enter opens Sessions, then Subagents, then tags the first subagent.
    fireEvent.keyDown(textarea(), { key: "Enter" });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    append("to compare with @alpha");
    fireEvent.click(screen.getByTitle("Tag session Alpha planning"));
    expect(textarea().value).toBe("Ask @auth to compare with @Alpha planning ");
    expect(tokens()).toEqual(["@auth", "@Alpha planning"]);

    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith(
      'Ask [Session: "auth" id=s_child subagent=yes] to compare with ' +
        `[Session: "Alpha planning" id=s_alpha subagent=no]\n\n${SESSION_MENTION_HINT}`,
      undefined,
    );
  });

  describe("recalling a queued tagged message", () => {
    const queuedText = () =>
      `Ask [Session: "Alpha planning" id=s_alpha subagent=no] to review\n\n${SESSION_MENTION_HINT}`;

    function renderWithQueued(onSend: (text: string) => void) {
      useChatStore.setState({
        queuedMessages: [{ queueId: "q_tagged", text: queuedText(), conversationId: currentId }],
      });
      renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    }

    it.each([
      ["Edit", () => fireEvent.click(screen.getByRole("button", { name: "Edit queued message" }))],
      ["ArrowUp", () => fireEvent.keyDown(textarea(), { key: "ArrowUp" })],
    ])("restores the tag as a token on %s, so a resend keeps one trailing hint", (_how, recall) => {
      const onSend = vi.fn();
      renderWithQueued(onSend);
      recall();
      expect(textarea().value).toBe("Ask @Alpha planning to review");
      expect(tokens()).toEqual(["@Alpha planning"]);
      append(" today");
      fireEvent.submit(textarea().closest("form")!);
      expect(onSend.mock.calls[0]![0]).toBe(
        `Ask [Session: "Alpha planning" id=s_alpha subagent=no] to review today\n\n${SESSION_MENTION_HINT}`,
      );
    });

    it("restores a failed send's tags as tokens, so a resend keeps one trailing hint", () => {
      const onSend = vi.fn();
      renderWithTooltips(<Composer {...composerProps({ onSend })} />);
      act(() =>
        useChatStore.setState({
          failedSendDraft: { conversationId: currentId, text: queuedText(), files: [] },
        }),
      );
      expect(textarea().value).toBe("Ask @Alpha planning to review");
      expect(tokens()).toEqual(["@Alpha planning"]);
      append(" today");
      fireEvent.submit(textarea().closest("form")!);
      expect(onSend.mock.calls[0]![0]).toBe(
        `Ask [Session: "Alpha planning" id=s_alpha subagent=no] to review today\n\n${SESSION_MENTION_HINT}`,
      );
    });

    it("merges a failed send into newer input without retargeting either tag", () => {
      const onSend = vi.fn();
      renderWithTooltips(<Composer {...composerProps({ onSend })} />);
      // Newer input already tags s_alpha as "@Alpha planning".
      type("first @alpha");
      fireEvent.click(screen.getByTitle("Tag session Alpha planning"));
      // A send recovered when the session loaded, which tagged a different
      // session with the same title, is added after the newer input.
      const failed =
        `Ask [Session: "Alpha planning" id=s_other subagent=no] to review\n\n` +
        SESSION_MENTION_HINT;
      act(() =>
        useChatStore.setState({
          failedSendDraft: { conversationId: currentId, text: failed, files: [], recovered: true },
        }),
      );
      expect(tokens()).toEqual(["@Alpha planning", "@Alpha planning (s_othe)"]);
      fireEvent.submit(textarea().closest("form")!);
      const sent = onSend.mock.calls[0]![0] as string;
      expect(sent).toBe(
        // The picked token keeps the space the composer adds after it.
        'first [Session: "Alpha planning" id=s_alpha subagent=no] \n\n' +
          'Ask [Session: "Alpha planning" id=s_other subagent=no] to review\n\n' +
          SESSION_MENTION_HINT,
      );
    });

    it("adds a newly tagged session to a recalled message with one hint", () => {
      const onSend = vi.fn();
      renderWithQueued(onSend);
      fireEvent.click(screen.getByRole("button", { name: "Edit queued message" }));
      append(" with @aut");
      fireEvent.click(screen.getByTitle("Tag subagent auth"));
      fireEvent.submit(textarea().closest("form")!);
      const sent = onSend.mock.calls[0]![0] as string;
      expect(sent).toBe(
        'Ask [Session: "Alpha planning" id=s_alpha subagent=no] to review with ' +
          `[Session: "auth" id=s_child subagent=yes]\n\n${SESSION_MENTION_HINT}`,
      );
      expect(sent.split("[Tagged sessions:")).toHaveLength(2);
    });
  });

  it("keeps tagged sessions when the composer remounts with the saved draft", () => {
    const onSend = vi.fn();
    const first = renderWithTooltips(<Composer {...composerProps()} />);
    type("Ask @alpha");
    fireEvent.click(screen.getByTitle("Tag session Alpha planning"));
    append("to review");
    // Leaving the page unmounts the composer; the draft is saved per session.
    first.unmount();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    expect(textarea().value).toBe("Ask @Alpha planning to review");
    expect(tokens()).toEqual(["@Alpha planning"]);
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend.mock.calls[0]![0]).toBe(
      `Ask [Session: "Alpha planning" id=s_alpha subagent=no] to review\n\n${SESSION_MENTION_HINT}`,
    );
  });

  it("inserts a mention picked mid-sentence in place and sends it there", async () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    const draft = "ask @alp to finish it";
    const caret = "ask @alp".length;
    fireEvent.change(textarea(), { target: { value: draft, selectionStart: caret } });
    fireEvent.click(screen.getByTitle("Tag session Alpha planning"));
    expect(textarea().value).toBe("ask @Alpha planning to finish it");
    // The caret lands after the token and the existing space.
    await waitFor(() => expect(textarea().selectionStart).toBe("ask @Alpha planning ".length));
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend.mock.calls[0]![0]).toBe(
      `ask [Session: "Alpha planning" id=s_alpha subagent=no] to finish it\n\n${SESSION_MENTION_HINT}`,
    );
  });

  it("keeps matching a session title as the query gains a space", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("Ask @alpha");
    type("Ask @alpha pl");
    // The typed part of the matching title is tinted, spaces included.
    expect(screen.getByTestId("mention-match").textContent).toBe("Alpha pl");
    fireEvent.click(screen.getByTitle("Tag session Alpha planning"));
    expect(textarea().value).toBe("Ask @Alpha planning ");
    // Once nothing matches, the space ends the token and the menu closes.
    type("Ask @alpha zz");
    expect(screen.queryByTestId("file-mention-menu")).not.toBeInTheDocument();
  });

  it("waits for typing to pause before searching, and searches the latest text", () => {
    vi.useFakeTimers();
    onTestFinished(() => {
      vi.useRealTimers();
    });
    sessions.searchHits = [sessionRow("s_remote", "Gamma rollout")];
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@g");
    act(() => vi.advanceTimersByTime(200));
    type("@gam");
    act(() => vi.advanceTimersByTime(299));
    // Rapid typing restarts the 300 ms wait, so nothing has been searched yet.
    expect(sessions.searchedQueries).toEqual([]);
    act(() => vi.advanceTimersByTime(1));
    expect(sessions.searchedQueries).toContain("gam");
    expect(sessions.searchedQueries).not.toContain("g");
    expect(screen.getByTitle("Tag session Gamma rollout")).toBeInTheDocument();
  });

  it("lists the Sessions row above files in a native session and navigates across both", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTestId("session-mention-category-sessions")).toHaveAttribute(
      "data-active",
      "true",
    );
    // The Sessions row, then the "src" folder.
    fireEvent.keyDown(textarea(), { key: "ArrowDown" });
    expect(screen.getByTestId("file-mention-item-0")).toHaveAttribute("data-active", "true");
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(tokens()).toEqual(["@src/"]);
  });

  it("matches no session for a path-shaped token", () => {
    useChatStore.setState({ sessionHarness: "claude-native" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@src/");
    expect(screen.queryAllByTestId(/^session-mention-item-/)).toHaveLength(0);
    expect(screen.getByTitle("Attach server.ts")).toBeInTheDocument();
  });
});
