import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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
}));
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
}
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useWorkspaceChangedFiles")>();
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
  };
});

import { Composer, detectMentionAt, mentionMarkerFor } from "./ChatPage";

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

function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

/** Type `text` into the composer with the caret at the end. */
function type(text: string) {
  fireEvent.change(textarea(), { target: { value: text, selectionStart: text.length } });
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

  it("opens a folder to reveal nested files, then delivers the chosen file", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    type("@src");
    // Nested files are NOT visible until the folder is opened (drill-down).
    expect(screen.queryByTitle("Attach server.ts")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTitle("Open src"));

    // Now inside src: nested files appear.
    fireEvent.click(screen.getByTitle("Attach server.ts"));
    expect(screen.getByText("@src/server.ts")).toBeInTheDocument();

    type("explain this");
    fireEvent.submit(textarea().closest("form")!);

    // The WHY: the agent reads the on-disk nested file from this exact marker.
    // No File is uploaded (second arg undefined).
    expect(onSend).toHaveBeenCalledWith("[Attached: src/server.ts]\n\nexplain this", undefined);
  });

  it("attaches a whole folder as a single trailing-slash marker", () => {
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    type("@src");
    fireEvent.click(screen.getByLabelText("Attach whole folder src"));
    expect(screen.getByText("@src/")).toBeInTheDocument();

    type("review the module");
    fireEvent.submit(textarea().closest("form")!);

    // A folder is delivered with a trailing "/" so the agent opens the dir.
    expect(onSend).toHaveBeenCalledWith("[Attached: src/]\n\nreview the module", undefined);
  });

  it.each([
    ["claude-native", "[Attached: src/server.ts]\n\ngo"],
    ["pi-native", "[Attached: src/server.ts]\n\ngo"],
    ["cursor-native", "[Attached: src/server.ts]\n\ngo"],
    ["codex-native", "[Attached file: src/server.ts]\n\ngo"],
  ])("delivers the harness-correct marker on %s", (harness, expected) => {
    useChatStore.setState({ sessionHarness: harness });
    const onSend = vi.fn();
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);
    type("@src");
    fireEvent.click(screen.getByTitle("Open src"));
    fireEvent.click(screen.getByTitle("Attach server.ts"));
    type("go");
    fireEvent.submit(textarea().closest("form")!);
    expect(onSend).toHaveBeenCalledWith(expected, undefined);
  });

  it("drains a file-viewer 'Attach to agent' line range into a chip and marker", () => {
    const onSend = vi.fn();
    // Simulate the file viewer's button having queued a line span.
    useChatStore.setState({
      pendingComposerAttachments: [
        { path: "bob-max-gain/docker-compose.yml", isDir: false, lineRange: { start: 2, end: 9 } },
      ],
    });
    renderWithTooltips(<Composer {...composerProps({ onSend })} />);

    // The composer drained the queue into a chip showing the path + lines
    // (the line span renders in its own node so it never truncates away)...
    expect(screen.getByText("@bob-max-gain/docker-compose.yml")).toBeInTheDocument();
    expect(screen.getByText(":2-9")).toBeInTheDocument();
    // ...and the store queue was cleared so it can't double-apply.
    expect(useChatStore.getState().pendingComposerAttachments).toEqual([]);

    type("explain this service");
    fireEvent.submit(textarea().closest("form")!);

    // Delivered as "path:start-end" inside the marker so the agent reads
    // exactly those lines.
    expect(onSend).toHaveBeenCalledWith(
      "[Attached: bob-max-gain/docker-compose.yml:2-9]\n\nexplain this service",
      undefined,
    );
  });

  it("does NOT show the mention menu on non-native (in-process SDK) harnesses", () => {
    useChatStore.setState({ sessionHarness: "claude-sdk" });
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@src");
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
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
    expect(screen.getByText("@readme.md")).toBeInTheDocument();
  });

  it("ArrowUp from the top row wraps to the last row", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // From row 0, ArrowUp wraps to the last row (readme.md), not -1.
    fireEvent.keyDown(textarea(), { key: "ArrowUp" });
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(screen.getByText("@readme.md")).toBeInTheDocument();
  });

  it("Enter on a directory row drills in rather than attaching", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // Row 0 is the "src" folder: Enter must open it (reveal nested files), and
    // must NOT produce a chip — drilling is navigation, not attachment. (The
    // token becomes "@src/" in the textarea; a chip would instead surface a
    // "Remove src" button, which must be absent.)
    fireEvent.keyDown(textarea(), { key: "Enter" });
    expect(screen.getByTitle("Attach server.ts")).toBeInTheDocument();
    expect(screen.queryByLabelText("Remove src")).not.toBeInTheDocument();
  });

  it("Tab attaches the highlighted folder as a whole-directory unit", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    // Tab on the "src" folder attaches it as a unit (trailing-slash chip),
    // distinct from Enter's drill-in.
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(screen.getByText("@src/")).toBeInTheDocument();
  });

  it("Escape closes the mention menu", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    expect(screen.getByTitle("Open src")).toBeInTheDocument();
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.queryByTitle("Open src")).not.toBeInTheDocument();
  });

  // ── Chip lifecycle + dedup (M8, M2) ──────────────────────────────────────

  it("removes a tagged chip when its ✕ is clicked", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    expect(screen.getByText("@readme.md")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Remove readme.md"));
    expect(screen.queryByText("@readme.md")).not.toBeInTheDocument();
  });

  it("dedups re-attaching the same file via '@' to a single chip", () => {
    renderWithTooltips(<Composer {...composerProps()} />);
    type("@");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    type("@");
    fireEvent.click(screen.getByTitle("Attach readme.md"));
    // Same path + dir-ness + (no) range → same identity key → one chip.
    expect(screen.getAllByText("@readme.md")).toHaveLength(1);
  });

  it("keeps two distinct line ranges of the same file as separate chips", () => {
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
    expect(screen.getAllByText("@a.ts")).toHaveLength(2);
    expect(screen.getByText(":2-9")).toBeInTheDocument();
    expect(screen.getByText(":20-30")).toBeInTheDocument();
  });

  it("collapses identical queued attachments to one chip on drain", () => {
    // The store dedups on insert, but the drain must also dedup within a batch
    // (and against already-tagged chips) so a duplicated queue can't double up.
    useChatStore.setState({
      pendingComposerAttachments: [
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
        { path: "a.ts", isDir: false, lineRange: { start: 2, end: 9 } },
      ],
    });
    renderWithTooltips(<Composer {...composerProps()} />);
    expect(screen.getAllByText(":2-9")).toHaveLength(1);
  });

  // ── Dismissal + loading feedback (M3, M1, M6) ─────────────────────────────

  it("dismisses the menu when the textarea loses focus", () => {
    // WHY (M3): the menu's visibility derives from the caret on change; without
    // a blur dismiss it lingers when the user clicks a chip ✕ or another field.
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
