// Regression guard for the save-after-teardown state leak (PR review, the
// handleSave "Force clean" comment block).
//
// The outer MarkdownRichTextViewer holds useMarkdownEditorSync; the inner
// editor (key={editorKey}) is torn down on a path switch / key remount / panel
// close while the sync hook PERSISTS. A save kicked off while the editor was
// alive can resolve AFTER teardown — the network write still lands, but its
// post-write mutations (markSaved / setDirty(false) / dismissExternalUpdate)
// must NOT run, or they leak this file's saved-state into whatever file the
// persistent hook now tracks (e.g. a late markSaved sets lastSavedRef for the
// next path and suppresses a legitimate update).
//
// Setup is a hybrid the other two suites don't cover: the fake TipTap editor
// drives the REAL useAutoSave wiring (so a save genuinely goes in flight),
// while useMarkdownEditorSync is mocked so its setters are spies we can assert
// did / didn't fire after unmount. Only the write is a controllable deferred.

import { StrictMode } from "react";
import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Thin fake TipTap editor: an event emitter over a mutable markdown string ---
interface FakeEditor {
  isDestroyed: boolean;
  getMarkdown: () => string;
  on: (evt: string, h: () => void) => void;
  off: (evt: string, h: () => void) => void;
  commands: { setContent: (c: string) => void };
  setEditable: () => void;
  state: { selection: { empty: boolean; from: number; to: number } };
  emit: (evt: string) => void;
  setMarkdown: (m: string) => void;
  isFocused: boolean;
  wired: boolean;
  // Latest onUpdate option. Real TipTap re-reads the current render's onUpdate on
  // every useEditor() call; under StrictMode the first (discarded) render's
  // closure would otherwise be pinned and reference a stale set of refs.
  latestOnUpdate: ((p: { editor: FakeEditor }) => void) | null;
}

function makeFakeEditor(initial: string): FakeEditor {
  const handlers: Record<string, Set<() => void>> = {};
  let markdown = initial;
  return {
    isDestroyed: false,
    getMarkdown: () => markdown,
    on: (evt, h) => { (handlers[evt] ??= new Set()).add(h); },
    off: (evt, h) => { handlers[evt]?.delete(h); },
    commands: { setContent: (c: string) => { markdown = c; } },
    setEditable: () => {},
    state: { selection: { empty: true, from: 0, to: 0 } },
    emit: (evt) => { handlers[evt]?.forEach((h) => h()); },
    setMarkdown: (m) => { markdown = m; },
    // Default focused: these tests simulate USER edits, which only fire 'update'
    // while the editor has focus (a pre-focus normalisation update re-baselines).
    isFocused: true,
    wired: false,
    latestOnUpdate: null,
  };
}

let fakeEditor: FakeEditor | null = null;

vi.mock("@tiptap/react", () => ({
  useEditor: (config: {
    onCreate?: (p: { editor: FakeEditor }) => void;
    onUpdate?: (p: { editor: FakeEditor }) => void;
  }) => {
    const f = fakeEditor;
    if (f) {
      // Bind to the latest render's callbacks (real TipTap reads the current
      // render's options too). Under StrictMode the first render is discarded —
      // its onCreate/onUpdate closures capture throwaway refs (baselineRef /
      // hasUserEditedRef). Pinning them (as a wire-once mock does) leaves the
      // COMMITTED render's refs unseeded: baseline stays null so the first user
      // edit hits the re-baseline branch and never flags the edit → no save.
      //
      // Mirror TipTap faithfully: re-run onCreate (re-seeds the current render's
      // baseline; it only reads getMarkdown(), so re-running is harmless) and
      // route the single "update" listener through the latest onUpdate.
      f.latestOnUpdate = config.onUpdate ?? null;
      config.onCreate?.({ editor: f });
      if (!f.wired) {
        f.wired = true;
        f.on("update", () => f.latestOnUpdate?.({ editor: f }));
      }
    }
    return f;
  },
  EditorContent: () => null,
}));

vi.mock("@tiptap/markdown", () => ({ Markdown: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/starter-kit", () => ({ default: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/extension-table", () => ({
  Table: { configure: vi.fn().mockReturnValue({}) },
  TableRow: {}, TableCell: {}, TableHeader: {},
}));
vi.mock("./TipTapGitHubAlert", () => ({ GitHubAlertBlockquote: {} }));
vi.mock("./TipTapHtmlPassthrough", () => ({ HtmlPassthrough: {} }));
vi.mock("./tiptapMarkdownPatches", () => ({ installMarkdownSerializerPatch: vi.fn() }));
vi.mock("./TipTapWorkspaceImage", () => ({
  createWorkspaceImageExtension: vi.fn().mockReturnValue({}),
  ImageAwareLink: { configure: vi.fn().mockReturnValue({}) },
}));
vi.mock("./TipTapCommentExtension", () => ({
  createCommentDecorationExtension: vi.fn().mockReturnValue({}),
  commentDecorationKey: {},
}));
vi.mock("./MarkdownCommentPlugin", () => ({ MarkdownCommentPlugin: () => null }));
vi.mock("./MarkdownEditorToolbar", () => ({ ToolbarPlugin: () => null }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn().mockReturnValue(true) }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));
// Sync hook is mocked here (unlike the integration suite) so its setters are spies.
vi.mock("./useMarkdownEditorSync", () => ({ useMarkdownEditorSync: vi.fn() }));

import { MarkdownRichTextViewer } from "./MarkdownRichTextViewer";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";
import * as syncHook from "./useMarkdownEditorSync";

const PATH = "/doc.md";
const INITIAL = "# Doc\n\ninitial body\n";
const EDITED = "# Doc\n\nedited body\n";

// Stable spies — must keep identity across renders so handleSave (which lists
// them in its dependency array) isn't recreated each render.
let setDirty: ReturnType<typeof vi.fn>;
let markSaved: ReturnType<typeof vi.fn>;
let dismissExternalUpdate: ReturnType<typeof vi.fn>;
let reconcileServerContent: ReturnType<typeof vi.fn>;

// Controllable in-flight write.
let resolveWrite: () => void;
let writePromise: Promise<void>;
let mutateAsync: ReturnType<typeof vi.fn>;

function makeViewer() {
  return (
    <MarkdownRichTextViewer
      content={INITIAL}
      conversationId="conv_teardown"  // not the focused/running session → no pre-write GET
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />
  );
}

beforeEach(() => {
  vi.useFakeTimers();
  fakeEditor = makeFakeEditor(INITIAL);

  setDirty = vi.fn();
  markSaved = vi.fn();
  dismissExternalUpdate = vi.fn();
  reconcileServerContent = vi.fn().mockReturnValue(false);
  vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue({
    editorKey: 1,
    isDirty: false,
    setDirty,
    hasExternalUpdate: false,
    discardAndApplyExternal: vi.fn(),
    dismissExternalUpdate,
    markSaved,
    reconcileServerContent,
  } as unknown as ReturnType<typeof syncHook.useMarkdownEditorSync>);

  writePromise = new Promise<void>((r) => { resolveWrite = r; });
  mutateAsync = vi.fn().mockReturnValue(writePromise);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false, isError: false, reset: vi.fn(), mutateAsync,
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);

  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MarkdownRichTextViewer save-after-teardown guard", () => {
  it("does NOT run post-write hook mutations when the write resolves after unmount", async () => {
    const { unmount } = render(makeViewer());
    // Edit → schedule → fire the debounce so a save goes in flight (awaiting the
    // deferred write); markSaved must not fire yet (write unresolved).
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => { fakeEditor!.emit("update"); });
    await act(async () => { vi.advanceTimersByTime(1100); });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
    expect(markSaved).not.toHaveBeenCalled();

    // Tear the editor down mid-write (real TipTap sets isDestroyed on unmount;
    // mirror that so the autosave dirty-check can't fire a trailing resave).
    fakeEditor!.isDestroyed = true;
    await act(async () => { unmount(); });

    // Write lands after teardown.
    await act(async () => { resolveWrite(); await writePromise; });

    // The leak: a late markSaved()/dismissExternalUpdate() would mutate the
    // persistent sync hook (now tracking another file). The mountedRef guard
    // must suppress them. Removing the guard makes both fire here → red.
    expect(markSaved).not.toHaveBeenCalled();
    expect(dismissExternalUpdate).not.toHaveBeenCalled();
  });

  it("DOES run post-write hook mutations for a save that resolves while still mounted", async () => {
    // Positive control: proves the negative test isn't vacuous — the same path
    // genuinely calls markSaved/setDirty(false) when the editor is still alive.
    render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => { fakeEditor!.emit("update"); });
    await act(async () => { vi.advanceTimersByTime(1100); });

    await act(async () => { resolveWrite(); await writePromise; });

    // Echo-dedupe marker recorded and editor forced clean — the normal save.
    expect(markSaved).toHaveBeenCalledWith(EDITED);
    expect(setDirty).toHaveBeenCalledWith(false);
    expect(dismissExternalUpdate).toHaveBeenCalledOnce();
  });

  it("still runs post-write mutations after a StrictMode mount/unmount/remount", async () => {
    // Regression: mountedRef must be set true in the effect SETUP, not only
    // cleared in cleanup. StrictMode (on in dev) runs mount→unmount→remount; a
    // cleanup-only ref sticks at false after that cycle, so every save's
    // post-write clear is skipped → stuck "Unsaved" + autosave loop. RTL's
    // render() isn't StrictMode by default, so this must be opted in explicitly.
    render(<StrictMode>{makeViewer()}</StrictMode>);
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => { fakeEditor!.emit("update"); });
    await act(async () => { vi.advanceTimersByTime(1100); });
    await act(async () => { resolveWrite(); await writePromise; });

    // With a cleanup-only mountedRef these are skipped (the guard bails on the
    // still-mounted editor because the ref is stuck false post-remount).
    expect(markSaved).toHaveBeenCalledWith(EDITED);
    expect(setDirty).toHaveBeenCalledWith(false);
  });
});
