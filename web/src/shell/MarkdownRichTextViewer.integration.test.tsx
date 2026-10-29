// Shallow integration test for the auto-save WIRING in MarkdownRichTextViewer:
//   editor "update" → debounced save, "blur" → flush, unmount → flush, and
//   runner-reconnect → flush.
//
// Unlike MarkdownRichTextViewer.test.tsx (which mocks useMarkdownEditorSync and
// useAutoSave), this exercises the REAL hooks through a thin fake editor that
// emits TipTap's update/blur events, so the glue between them is covered. Only
// the write endpoint (useWriteFileContent) is mocked, to assert the PUT fires
// with the edited content.

import { Schema, type Node } from "@tiptap/pm/model";
import type * as TipTapEditorHelpers from "./TipTapEditorHelpers";
import type * as FileContentModule from "@/hooks/useFileContent";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// --- Thin fake TipTap editor: an event emitter over a mutable markdown string ---
// Mirrors TipTap's "update" event carrying a transaction, so tests can emit a
// no-op (docChanged:false) or flagged transaction.
interface FakeTr {
  docChanged: boolean;
  getMeta: (key: string) => unknown;
}
interface UpdatePayload {
  transaction?: FakeTr;
}
type UpdateHandler = (payload?: UpdatePayload) => void;

interface FakeEditor {
  isDestroyed: boolean;
  getMarkdown: () => string;
  on: (evt: string, h: UpdateHandler) => void;
  off: (evt: string, h: UpdateHandler) => void;
  setEditable: () => void;
  state: { doc: Node; selection: { empty: boolean; from: number; to: number } };
  emit: (evt: string, payload?: UpdatePayload) => void;
  setMarkdown: (m: string) => void;
  isFocused: boolean;
  wired: boolean;
}

const schema = new Schema({ nodes: { doc: { content: "text*" }, text: {} } });
const documentFor = (text: string) =>
  schema.node("doc", null, text ? schema.text(text) : undefined);

function makeFakeEditor(initial: string): FakeEditor {
  const handlers: Record<string, Set<UpdateHandler>> = {};
  let markdown = initial;
  const state = { doc: documentFor(initial), selection: { empty: true, from: 0, to: 0 } };
  return {
    isDestroyed: false,
    getMarkdown: () => markdown,
    on: (evt, h) => {
      (handlers[evt] ??= new Set()).add(h);
    },
    off: (evt, h) => {
      handlers[evt]?.delete(h);
    },
    setEditable: () => {},
    state,
    emit: (evt, payload) => {
      handlers[evt]?.forEach((h) => h(payload));
    },
    setMarkdown: (m) => {
      markdown = m;
      state.doc = documentFor(m);
    },
    // Default focused: these tests simulate USER edits, which only happen while
    // the editor has focus. The load-normalisation test flips this to false.
    isFocused: true,
    wired: false,
  };
}

let fakeEditor: FakeEditor | null = null;

vi.mock("@tiptap/react", () => ({
  useEditor: (config: {
    onCreate?: (p: { editor: FakeEditor }) => void;
    onUpdate?: (p: { editor: FakeEditor; transaction?: FakeTr }) => void;
  }) => {
    const f = fakeEditor;
    if (f && !f.wired) {
      f.wired = true;
      // onCreate sets the editor's baseline; real TipTap also registers the
      // onUpdate option as an "update" listener carrying the transaction.
      config.onCreate?.({ editor: f });
      if (config.onUpdate) {
        f.on("update", (p) => config.onUpdate!({ editor: f, transaction: p?.transaction }));
      }
    }
    return f;
  },
  EditorContent: () => null,
}));

// Extensions / sibling components the viewer imports — irrelevant to the wiring.
vi.mock("@tiptap/markdown", () => ({ Markdown: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/starter-kit", () => ({ StarterKit: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/extension-table", () => ({
  Table: { configure: vi.fn().mockReturnValue({}) },
  TableRow: {},
  TableCell: {},
  TableHeader: {},
}));
vi.mock("./tiptapBoundedTokenizers", () => ({
  BoundedTaskList: {},
  BoundedTable: { configure: vi.fn().mockReturnValue({}) },
}));
vi.mock("./TipTapGitHubAlert", () => ({ GitHubAlertBlockquote: {} }));
vi.mock("./TipTapHtmlPassthrough", () => ({ HtmlPassthrough: {} }));
vi.mock("./tiptapMarkdownPatches", () => ({
  installMarkdownSerializerPatch: vi.fn(),
  installMarkdownParserPatch: vi.fn(),
}));
vi.mock("./TipTapWorkspaceImage", () => ({
  createWorkspaceImageExtension: vi.fn().mockReturnValue({}),
  ImageAwareLink: { configure: vi.fn().mockReturnValue({}) },
}));
vi.mock("./TipTapCommentExtension", () => ({
  createCommentDecorationExtension: vi.fn().mockReturnValue({}),
  commentDecorationKey: {},
}));
vi.mock("./MarkdownCommentPlugin", () => ({ MarkdownCommentPlugin: () => null }));
// The fake editor has no schema or Markdown parser: an external update simply
// replaces its Markdown (TipTapEditorHelpers.test.ts covers the real editor).
vi.mock("./TipTapEditorHelpers", async (importOriginal) => ({
  ...(await importOriginal<typeof TipTapEditorHelpers>()),
  applyExternalMarkdown: (editor: FakeEditor, markdown: string) => editor.setMarkdown(markdown),
}));
// Capture the onSave the viewer wires so a test can fire a "manual save" (⌘S / pill).
const toolbar = vi.hoisted(() => ({ onSave: null as ((md: string) => void) | null }));
vi.mock("./MarkdownEditorToolbar", () => ({
  ToolbarPlugin: (props: { onSave: (md: string) => void }) => {
    toolbar.onSave = props.onSave;
    return null;
  },
}));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn().mockReturnValue(true) }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
// The pre-write conflict check GETs the live file before every save.
vi.mock("@/hooks/useFileContent", async (importOriginal) => ({
  ...(await importOriginal<typeof FileContentModule>()),
  fetchFileContent: vi.fn(),
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
  useSessionHostOnline: vi.fn(),
}));

import { MarkdownRichTextViewer } from "./MarkdownRichTextViewer";
import { CODE_BLOCK_LANGUAGE_EDIT_META } from "./codeBlockLanguageEdit";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";
import { fetchFileContent } from "@/hooks/useFileContent";

const PATH = "/doc.md";
const INITIAL = "# Doc\n\ninitial body\n";
const EDITED = "# Doc\n\nedited body\n";
// Round-trip drift with no user edit. It sits inside the document, so saving's
// final-newline normalization can't hide it.
const DRIFTED = INITIAL.replace("\n\n", "\n\n\n");

let mutateAsync: ReturnType<typeof vi.fn>;

function mockWrite(): void {
  mutateAsync = vi.fn().mockResolvedValue(undefined);
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    reset: vi.fn(),
    mutateAsync,
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
}

// A fresh element each call: passing the same element reference to rerender()
// makes React bail out (identical-element optimization), which would skip
// re-reading the runner-online mock in the reconnect test.
function makeViewer(content: string = INITIAL) {
  return (
    <MarkdownRichTextViewer
      content={content}
      conversationId="conv_int"
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
  toolbar.onSave = null;
  mockWrite();
  // The pre-write conflict check finds the content the viewer loaded.
  vi.mocked(fetchFileContent).mockResolvedValue({
    object: "session.environment.filesystem.file_content",
    path: PATH,
    content_type: "text/markdown",
    encoding: "utf-8",
    content: INITIAL,
    bytes: INITIAL.length,
  });
  // Online → auto-save enabled.
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MarkdownRichTextViewer auto-save wiring (integration)", () => {
  it("serializes once at save time, never for each keystroke or a clean blur", async () => {
    render(makeViewer());
    const serialize = vi.spyOn(fakeEditor!, "getMarkdown");
    await act(async () => {
      for (const suffix of ["a", "ab", "abc"]) {
        fakeEditor!.setMarkdown(INITIAL + suffix);
        fakeEditor!.emit("update");
      }
    });
    expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument();
    expect(serialize).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(serialize).toHaveBeenCalledTimes(1);
    // The file ended with a newline, so the save keeps one.
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ path: PATH, content: `${INITIAL}abc\n` });
    await act(async () => fakeEditor!.emit("blur"));
    expect(serialize).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
  });

  it("saves one final newline when the editor serialises trailing blank lines", async () => {
    render(makeViewer());
    await act(async () => {
      fakeEditor!.setMarkdown(`${EDITED}\n\n`);
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ path: PATH, content: EDITED });
  });

  it("does not add a final newline the file did not have", async () => {
    const withoutNewline = INITIAL.replace(/\n$/, "");
    render(makeViewer(withoutNewline));
    await act(async () => {
      fakeEditor!.setMarkdown(`${withoutNewline} more`);
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({
      path: PATH,
      content: `${withoutNewline} more`,
    });
  });

  it("undo to an equivalent baseline document clears dirty without serializing or writing", async () => {
    render(makeViewer());
    const original = fakeEditor!.state.doc;
    const serialize = vi.spyOn(fakeEditor!, "getMarkdown");
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED);
      fakeEditor!.emit("update");
    });
    expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument();
    await act(async () => {
      fakeEditor!.setMarkdown(INITIAL);
      fakeEditor!.emit("update");
    });
    expect(fakeEditor!.state.doc).not.toBe(original);
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(1000);
      fakeEditor!.emit("blur");
    });
    expect(serialize).not.toHaveBeenCalled();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("re-baselines a changed document that serializes to the same Markdown without writing", async () => {
    render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    const serialize = vi.spyOn(fakeEditor!, "getMarkdown").mockReturnValue(INITIAL);
    await act(async () => fakeEditor!.emit("update"));
    await act(async () => vi.advanceTimersByTime(1000));
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
    await act(async () => fakeEditor!.emit("blur"));
    expect(serialize).toHaveBeenCalledTimes(1);
  });

  it("preserves edits made during a pending write and baselines the saved snapshot", async () => {
    let resolveWrite!: () => void;
    mutateAsync.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveWrite = resolve;
        }),
    );
    render(makeViewer());
    const serialize = vi.spyOn(fakeEditor!, "getMarkdown");
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED);
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED + "newer");
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    expect(mutateAsync).toHaveBeenCalledTimes(1);
    expect(serialize).toHaveBeenCalledTimes(1);
    await act(async () => resolveWrite());
    expect(mutateAsync).toHaveBeenNthCalledWith(2, { path: PATH, content: `${EDITED}newer\n` });
    expect(serialize).toHaveBeenCalledTimes(2);
    await act(async () => fakeEditor!.emit("blur"));
    expect(mutateAsync).toHaveBeenCalledTimes(2);
    expect(serialize).toHaveBeenCalledTimes(2);
  });

  it("keeps edits typed during a save when another writer's update arrives", async () => {
    let resolveWrite!: () => void;
    mutateAsync.mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolveWrite = resolve;
        }),
    );
    const { rerender } = render(makeViewer());
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED);
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    // Typed while the save is in flight; its own debounce hasn't fired yet.
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED + "newer");
      fakeEditor!.emit("update");
    });
    await act(async () => resolveWrite());
    await act(async () => rerender(makeViewer("# Doc\n\nanother writer\n")));

    expect(fakeEditor!.getMarkdown()).toBe(EDITED + "newer");
    expect(screen.getByText(/modified externally/)).toBeInTheDocument();
    await act(async () => vi.advanceTimersByTime(1000));
    // Unresolved conflict: auto-save waits for Keep mine / Load latest.
    expect(mutateAsync).toHaveBeenCalledExactlyOnceWith({ path: PATH, content: EDITED });
  });

  it("uses an external replacement as the new undo baseline", async () => {
    const { rerender } = render(makeViewer());
    await act(async () => rerender(makeViewer(EDITED)));
    expect(fakeEditor!.getMarkdown()).toBe(EDITED);
    const serialize = vi.spyOn(fakeEditor!, "getMarkdown");
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED + "newer");
      fakeEditor!.emit("update");
    });
    await act(async () => {
      fakeEditor!.setMarkdown(EDITED);
      fakeEditor!.emit("update");
      vi.advanceTimersByTime(1000);
    });
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
    expect(serialize).not.toHaveBeenCalled();
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("debounced save fires after an edit (update → schedule → write)", async () => {
    render(makeViewer());
    // Edit: content diverges from the onCreate baseline, then emit 'update'.
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    // Still within the debounce window — nothing written yet.
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    // If this fails the update→schedule→runSave→handleSave chain is broken.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("blur flushes the pending save before the debounce elapses", async () => {
    render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => {
      fakeEditor!.emit("blur");
    });
    // Blur must flush immediately — a missing editor.on("blur", flush) wiring
    // would leave this uncalled until the timer fired.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("unmount flushes pending edits", async () => {
    const { unmount } = render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    await act(async () => {
      unmount();
    });
    // The unmount cleanup must flush so a file-switch mid-debounce isn't lost.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("does NOT flush a load-time normalization drift on unmount (open+close ≠ edit)", async () => {
    // The open-rewrites-file bug. TipTap's markdown round-trip isn't byte-stable,
    // so getMarkdown() can drift from the captured baseline with NO user edit —
    // e.g. baseline captured against a different editor instance across
    // StrictMode's mount→unmount→remount. The live dirty check used by the
    // unmount/blur flush must require a real (focused) edit, or merely opening
    // then closing a file silently rewrites it with the normalised serialisation.
    const { unmount } = render(makeViewer());
    fakeEditor!.isFocused = false; // user never clicked in
    fakeEditor!.setMarkdown(DRIFTED); // drift, but no "update" emitted
    // Unmount (panel close / file switch) flushes — it must find nothing to
    // save. Before the fix the live dirty check (getMarkdown ≠ baseline) fired a
    // PUT of the normalised content here.
    await act(async () => {
      unmount();
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("keeps a blurred language change when a no-op update (setEditable) fires mid-debounce", async () => {
    // The picker dispatches a flagged, doc-changing update while blurred (the
    // <select> holds focus). If canEdit then flips within the debounce window,
    // editor.setEditable emits an empty (docChanged:false) update — which must
    // NOT re-baseline over the pending change, or the queued save no-ops and the
    // language change is silently lost on reload.
    render(makeViewer());
    fakeEditor!.isFocused = false;
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update", {
        transaction: { docChanged: true, getMeta: (k) => k === CODE_BLOCK_LANGUAGE_EDIT_META },
      });
    });
    // Synthetic no-op update from setEditable — the bug re-baselined here.
    await act(async () => {
      fakeEditor!.emit("update", {
        transaction: { docChanged: false, getMeta: () => undefined },
      });
    });
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("marks the editor clean after a save even if getMarkdown() drifts (no stuck 'Unsaved')", async () => {
    // TipTap's Markdown round-trip isn't byte-stable: the write persists X, then
    // getMarkdown() returns X plus whitespace with no user edit. The post-save
    // check compares document snapshots, so a successful save still ends clean.
    const saved: string[] = [];
    vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
      isPending: false,
      isError: false,
      reset: vi.fn(),
      mutateAsync: vi.fn(async ({ content }: { content: string }) => {
        saved.push(content);
        vi.spyOn(fakeEditor!, "getMarkdown").mockReturnValue(content.replace("\n\n", "\n\n\n"));
      }),
    } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);

    render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    expect(screen.getByText(/Unsaved changes/)).toBeInTheDocument(); // dirty before the save
    await act(async () => {
      vi.advanceTimersByTime(1100);
    });

    // Saved once, and clean afterward despite getMarkdown() != the saved text.
    expect(saved).toEqual([EDITED]);
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
  });

  it("does NOT save an unfocused load-time normalization update (open ≠ edit)", async () => {
    // On open, TipTap parses then re-serialises the doc and fires 'update'
    // before the user focuses; its markdown round-trip isn't byte-stable, so
    // getMarkdown() drifts from the on-disk content. That drift must NOT be
    // treated as an edit and autosaved — merely viewing a file must never
    // rewrite it. Regression for the silent normalize-on-open writes.
    render(makeViewer());
    fakeEditor!.isFocused = false; // user hasn't clicked in yet
    fakeEditor!.setMarkdown(DRIFTED); // round-trip drift, not a user edit
    await act(async () => {
      fakeEditor!.emit("update");
    });
    await act(async () => {
      vi.advanceTimersByTime(1100);
    });
    // If onUpdate flagged this dirty (or the wiring scheduled it), a spurious
    // write fires here — the exact open-rewrites-file bug.
    expect(mutateAsync).not.toHaveBeenCalled();
    // And the editor stays clean (no "Unsaved" pill on a file you only opened).
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
  });

  it("manual save coalesces with an in-flight auto-save (single-flight, no overlapping PUT)", async () => {
    // A manual save (⌘S / pill) must route through the same single-flight engine
    // as auto-save: with a write already in flight, it must NOT start a second
    // concurrent PUT. The old wiring (onSave={handleSave}) called the writer
    // directly and fired two overlapping PUTs.
    let resolveWrite!: () => void;
    const writePromise = new Promise<void>((r) => {
      resolveWrite = r;
    });
    let calls = 0;
    vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
      isPending: false,
      isError: false,
      reset: vi.fn(),
      mutateAsync: vi.fn(() => {
        calls++;
        return writePromise;
      }),
    } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);

    render(makeViewer());
    fakeEditor!.isFocused = true;
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    await act(async () => {
      vi.advanceTimersByTime(1100);
    }); // auto-save now in flight
    expect(calls).toBe(1);

    // Manual save while the auto-save PUT is still unresolved.
    await act(async () => {
      toolbar.onSave!(EDITED);
    });
    // Single-flight coalesced it — still one write. Old wiring → 2.
    expect(calls).toBe(1);

    // Settle: baseline now equals the saved content, so no trailing resave fires.
    await act(async () => {
      resolveWrite();
      await writePromise;
    });
    expect(calls).toBe(1);
  });

  it("flushes accumulated edits when the runner reconnects", async () => {
    // Runner down, no host to serve the workspace → auto-save suppressed.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    vi.mocked(runnerHook.useSessionHostOnline).mockReturnValue(null);
    const { rerender } = render(makeViewer());
    fakeEditor!.setMarkdown(EDITED);
    await act(async () => {
      fakeEditor!.emit("update");
    });
    // Dirty, but offline → no write attempted.
    expect(mutateAsync).not.toHaveBeenCalled();

    // Runner reconnects → the re-enable effect flushes the queued edits.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
    await act(async () => {
      rerender(makeViewer());
    });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });
});
