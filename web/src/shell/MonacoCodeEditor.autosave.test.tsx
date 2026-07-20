// Shallow integration test for the auto-save WIRING in MonacoCodeEditor:
//   onChange (edit) → debounced save, blur → flush, unmount → flush,
//   ⌘S → single-flight flush, and runner-reconnect → flush.
//
// Like MarkdownRichTextViewer.integration.test.tsx, this exercises the REAL
// useMarkdownEditorSync + useAutoSave hooks. Monaco can't mount in jsdom, so
// @monaco-editor/react's Editor is mocked to invoke onMount with a thin fake
// editor and capture the onChange the component wires. The comment layer is
// mocked out (irrelevant to save wiring); only the write endpoint is mocked, to
// assert the PUT fires with the edited content.

import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Captured callbacks the component registers on the editor, so tests can drive
// edits / blur / ⌘S. Hoisted because the @monaco-editor/react mock factory
// (also hoisted) closes over them.
const h = vi.hoisted(() => ({
  onChange: null as ((value: string | undefined, ev: unknown) => void) | null,
  cmdS: null as (() => void) | null,
  blur: null as (() => void) | null,
}));

// Minimal Monaco namespace: only the members handleMount touches.
const fakeMonaco = {
  editor: { EndOfLineSequence: { LF: 1, CRLF: 2 } },
  KeyMod: { CtrlCmd: 2048 },
  KeyCode: { KeyS: 49 },
};

interface FakeEditor {
  getValue: () => string;
  setValue: (v: string) => void;
  getModel: () => { setEOL: () => void };
  addCommand: (binding: number, handler: () => void) => void;
  onDidBlurEditorWidget: (handler: () => void) => { dispose: () => void };
  saveViewState: () => null;
  restoreViewState: () => void;
  getAction: () => { run: () => void };
  getContribution: () => null;
  /** Test-only: set getValue() without firing onChange (mirrors a user keystroke
   *  landing in the buffer; the test fires onChange separately). */
  __set: (v: string) => void;
}

function makeFakeEditor(initial: string): FakeEditor {
  let value = initial;
  return {
    getValue: () => value,
    // Real Monaco fires onDidChangeModelContent (→ onChange) on setValue; mirror
    // that so the component's setContentRef path is faithful.
    setValue: (v) => {
      value = v;
      h.onChange?.(v, { isFlush: true });
    },
    getModel: () => ({ setEOL: () => {} }),
    addCommand: (_binding, handler) => {
      h.cmdS = handler;
    },
    onDidBlurEditorWidget: (handler) => {
      h.blur = handler;
      return { dispose: () => {} };
    },
    saveViewState: () => null,
    restoreViewState: () => {},
    getAction: () => ({ run: () => {} }),
    getContribution: () => null,
    __set: (v) => {
      value = v;
    },
  };
}

let fakeEditor: FakeEditor | null = null;

// Editor mock: stash onChange, then invoke onMount once from a passive effect
// (calling it during render would update the parent mid-render). Async factory
// so it can pull useEffect without referencing a top-level import.
vi.mock("@monaco-editor/react", async () => {
  const { useEffect } = await import("react");
  return {
    Editor: (props: {
      onMount?: (editor: unknown, monaco: unknown) => void;
      onChange?: (value: string | undefined, ev: unknown) => void;
    }) => {
      h.onChange = props.onChange ?? null;
      useEffect(() => {
        props.onMount?.(fakeEditor, fakeMonaco);
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);
      return null;
    },
  };
});

// Resolve the Shiki/Monaco setup so the inner component flips `ready` and
// renders the (mocked) Editor.
vi.mock("./monacoSetup", () => ({
  ensureMonacoReady: vi.fn(() => Promise.resolve()),
  ensureLanguage: vi.fn(() => Promise.resolve()),
  monacoLanguageId: vi.fn((lang: string) => lang),
  resolvedThemeToMonaco: vi.fn(() => "github-light"),
}));
// Comment layer is unrelated to save wiring and needs a large editor surface.
vi.mock("./useMonacoCommentLayer", () => ({ useMonacoCommentLayer: () => null }));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn().mockReturnValue(true) }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));

import { MonacoCodeEditor } from "./MonacoCodeEditor";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

const PATH = "src/a.ts";
const INITIAL = "const x = 1;\n";
const EDITED = "const x = 2;\n";

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

// Fresh element each call so rerender() doesn't bail on an identical reference
// (the reconnect test relies on re-reading the runner-online mock).
function makeEditor() {
  return (
    <MonacoCodeEditor
      content={INITIAL}
      // Not the focused/running session → pre-write conflict GET is skipped.
      conversationId="conv_monaco_autosave"
      path={PATH}
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />
  );
}

// Render and flush the ready promise so <Editor> mounts and onMount fires.
async function renderMounted(el: React.ReactElement) {
  const utils = render(el);
  await act(async () => {});
  return utils;
}

// Drive a user edit: update the buffer, then fire the captured onChange.
async function fireEdit(value: string) {
  fakeEditor!.__set(value);
  await act(async () => {
    h.onChange?.(value, {});
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  fakeEditor = makeFakeEditor(INITIAL);
  h.onChange = null;
  h.cmdS = null;
  h.blur = null;
  mockWrite();
  // Online → auto-save enabled.
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  fakeEditor = null;
});

describe("MonacoCodeEditor auto-save wiring (integration)", () => {
  it("debounced save fires after an edit (onChange → schedule → write)", async () => {
    await renderMounted(makeEditor());
    // onMount must have registered the ⌘S command and blur listener.
    expect(h.cmdS).not.toBeNull();
    expect(h.blur).not.toBeNull();

    await fireEdit(EDITED);
    // Still inside the debounce window — nothing written yet.
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    // If this fails the onChange→schedule→runSave→handleSave chain is broken.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
    // Exactly one write — a debounce regression that fires per-keystroke (or
    // double-schedules) would push this above 1.
    expect(mutateAsync).toHaveBeenCalledTimes(1);
  });

  it("does NOT auto-save when a file is merely opened (no edit)", async () => {
    await renderMounted(makeEditor());
    // No onChange fired. Monaco's getValue is byte-stable, so opening a file
    // never produces a spurious dirty/normalisation write.
    await act(async () => {
      vi.advanceTimersByTime(2000);
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("blur flushes the pending save before the debounce elapses", async () => {
    await renderMounted(makeEditor());
    await fireEdit(EDITED);
    expect(mutateAsync).not.toHaveBeenCalled();
    await act(async () => {
      h.blur!();
    });
    // Blur must flush immediately — a missing onDidBlurEditorWidget→flush wiring
    // would leave this uncalled until the timer fired.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("unmount flushes pending edits", async () => {
    const { unmount } = await renderMounted(makeEditor());
    await fireEdit(EDITED);
    await act(async () => {
      unmount();
    });
    // The unmount cleanup must flush so a file-switch mid-debounce isn't lost.
    // Reads latestContentRef (not the disposed editor), so it still has the text.
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });

  it("manual ⌘S coalesces with an in-flight auto-save (single-flight, no overlapping PUT)", async () => {
    // A manual save must route through the same single-flight engine as
    // auto-save: with a write already in flight it must NOT start a second
    // concurrent PUT. Calling the writer directly from the ⌘S command would.
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

    await renderMounted(makeEditor());
    await fireEdit(EDITED);
    await act(async () => {
      vi.advanceTimersByTime(1000);
    }); // auto-save now in flight
    expect(calls).toBe(1);

    // Manual ⌘S while the auto-save PUT is still unresolved.
    await act(async () => {
      h.cmdS!();
    });
    // Single-flight coalesced it — still one write. A direct call → 2.
    expect(calls).toBe(1);

    // Settle: baseline now equals the saved content, so no trailing resave.
    await act(async () => {
      resolveWrite();
      await writePromise;
    });
    expect(calls).toBe(1);
  });

  it("flushes accumulated edits when the runner reconnects", async () => {
    // Offline → auto-save suppressed.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    const { rerender } = await renderMounted(makeEditor());
    await fireEdit(EDITED);
    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    // Dirty, but offline → the scheduled save bails (enabled=false).
    expect(mutateAsync).not.toHaveBeenCalled();

    // Runner reconnects → the re-enable effect flushes the queued edit.
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(true);
    await act(async () => {
      rerender(makeEditor());
    });
    expect(mutateAsync).toHaveBeenCalledWith({ path: PATH, content: EDITED });
  });
});
