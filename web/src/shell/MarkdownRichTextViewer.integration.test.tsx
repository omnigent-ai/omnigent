// Shallow integration test for the auto-save WIRING in MarkdownRichTextViewer:
//   editor "update" → debounced save, "blur" → flush, unmount → flush, and
//   runner-reconnect → flush.
//
// Unlike MarkdownRichTextViewer.test.tsx (which mocks useMarkdownEditorSync and
// useAutoSave), this exercises the REAL hooks through a thin fake editor that
// emits TipTap's update/blur events, so the glue between them is covered. Only
// the write endpoint (useWriteFileContent) is mocked, to assert the PUT fires
// with the edited content.

import { act, render, screen } from "@testing-library/react";
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
}

function makeFakeEditor(initial: string): FakeEditor {
  const handlers: Record<string, Set<() => void>> = {};
  let markdown = initial;
  return {
    isDestroyed: false,
    getMarkdown: () => markdown,
    on: (evt, h) => {
      (handlers[evt] ??= new Set()).add(h);
    },
    off: (evt, h) => {
      handlers[evt]?.delete(h);
    },
    commands: {
      setContent: (c: string) => {
        markdown = c;
      },
    },
    setEditable: () => {},
    state: { selection: { empty: true, from: 0, to: 0 } },
    emit: (evt) => {
      handlers[evt]?.forEach((h) => h());
    },
    setMarkdown: (m) => {
      markdown = m;
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
    onUpdate?: (p: { editor: FakeEditor }) => void;
  }) => {
    const f = fakeEditor;
    if (f && !f.wired) {
      f.wired = true;
      // onCreate sets the editor's baseline; real TipTap also registers the
      // onUpdate option as an "update" listener, so mirror that.
      config.onCreate?.({ editor: f });
      if (config.onUpdate) f.on("update", () => config.onUpdate!({ editor: f }));
    }
    return f;
  },
  EditorContent: () => null,
}));

// Extensions / sibling components the viewer imports — irrelevant to the wiring.
vi.mock("@tiptap/markdown", () => ({ Markdown: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/starter-kit", () => ({ default: { configure: vi.fn().mockReturnValue({}) } }));
vi.mock("@tiptap/extension-table", () => ({
  Table: { configure: vi.fn().mockReturnValue({}) },
  TableRow: {},
  TableCell: {},
  TableHeader: {},
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
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));

import { MarkdownRichTextViewer } from "./MarkdownRichTextViewer";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const PATH = "/doc.md";
const INITIAL = "# Doc\n\ninitial body\n";
const EDITED = "# Doc\n\nedited body\n";

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
  // Online → auto-save enabled. The session is idle (this conversation isn't
  // the focused running one), so the pre-write conflict GET is skipped and the
  // write goes straight through.
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MarkdownRichTextViewer auto-save wiring (integration)", () => {
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
    fakeEditor!.setMarkdown(INITIAL + "\n"); // drift, but no "update" emitted
    // Unmount (panel close / file switch) flushes — it must find nothing to
    // save. Before the fix the live dirty check (getMarkdown ≠ baseline) fired a
    // PUT of the normalised content here.
    await act(async () => {
      unmount();
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("marks the editor clean after a save even if getMarkdown() drifts (no stuck 'Unsaved')", async () => {
    // Regression guard: TipTap's markdown round-trip is NOT byte-stable, so
    // re-deriving dirtiness from a live getMarkdown()-vs-baseline string
    // compare after a save (instead of forcing clean) left the editor stuck on
    // "Unsaved" and looping autosave. Simulate the drift: the write persists X,
    // but the editor's getMarkdown afterward is X + whitespace (≠ X) — with no
    // user edit. A successful save must still end clean.
    const saved: string[] = [];
    vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
      isPending: false,
      isError: false,
      reset: vi.fn(),
      mutateAsync: vi.fn(async ({ content }: { content: string }) => {
        saved.push(content);
        fakeEditor!.setMarkdown(content + "\n"); // round-trip drift, not a user edit
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
    // With setDirty(isEditorDirty()) this would be stuck "Unsaved".
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
    fakeEditor!.setMarkdown(INITIAL + "\n"); // round-trip drift, not a user edit
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
    // Offline → auto-save suppressed.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
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
