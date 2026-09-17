// Tests for MonacoDiffViewer's DiffEditor wiring. Monaco can't mount in jsdom,
// so @monaco-editor/react's DiffEditor is mocked to capture the props it
// receives; we assert the original/modified content and layout→renderSideBySide
// mapping. The comment layer is exercised by MonacoCodeEditor / buildCommentDecorations.

import { act } from "react";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DiffOnMount } from "@monaco-editor/react";

const h = vi.hoisted(() => ({
  diffProps: null as {
    original?: string;
    modified?: string;
    options?: {
      renderSideBySide?: boolean;
      scrollBeyondLastLine?: boolean;
      readOnly?: boolean;
      hideUnchangedRegions?: { enabled?: boolean };
      ignoreTrimWhitespace?: boolean;
      diffWordWrap?: "on" | "off";
      fontWeight?: string;
    };
  } | null,
  onMount: null as DiffOnMount | null,
  commentOptions: null as { editorRef: { current: unknown }; mounted: boolean } | null,
}));
vi.mock("@monaco-editor/react", () => ({
  DiffEditor: (props: {
    original?: string;
    modified?: string;
    options?: Record<string, unknown>;
    onMount?: DiffOnMount;
  }) => {
    h.diffProps = props;
    h.onMount = props.onMount ?? null;
    return null;
  },
}));
vi.mock("./monacoSetup", () => ({
  ensureMonacoReady: vi.fn(() => Promise.resolve()),
  ensureLanguage: vi.fn(() => Promise.resolve()),
  monacoLanguageId: vi.fn((lang: string) => lang),
  resolvedThemeToMonaco: vi.fn(() => "github-light"),
}));
// Capture the comment-layer options so we can assert the diff wires the
// modified editor into it; return null so render works.
vi.mock("./useMonacoCommentLayer", () => ({
  useMonacoCommentLayer: (opts: { editorRef: { current: unknown }; mounted: boolean }) => {
    h.commentOptions = opts;
    return null;
  },
}));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn(() => true) }));

import { MonacoDiffViewer } from "./MonacoDiffViewer";
import { codeFontFamilyForEditor, writeCodeFontSizePx } from "@/lib/codeFontPreferences";
import { getSavedScrollTop, saveScrollTop } from "./useScrollRestore";

function diffTree(props: {
  position?: { line: number; column?: number };
  before: string | null;
  after: string | null;
  layout: "unified" | "split";
  hideWhitespace?: boolean;
  wrapLines?: boolean;
  searchOpen?: boolean;
  onSearchHandled?: () => void;
}) {
  return (
    <MonacoDiffViewer
      position={props.position}
      before={props.before}
      after={props.after}
      path="src/a.ts"
      layout={props.layout}
      hideWhitespace={props.hideWhitespace ?? false}
      wrapLines={props.wrapLines ?? false}
      conversationId="conv_1"
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      searchOpen={props.searchOpen}
      onSearchHandled={props.onSearchHandled}
    />
  );
}

function renderDiff(props: Parameters<typeof diffTree>[0]) {
  return render(diffTree(props));
}

// Fake modified-side editor with the find slice the search wiring drives:
// getAction("actions.find").run() and getContribution(findController).
function makeFindController() {
  let listener: ((e: { isRevealed: boolean }) => void) | null = null;
  const controller = {
    isRevealed: false,
    run: vi.fn(() => {
      controller.isRevealed = true;
    }),
    closeFindWidget: vi.fn(() => {
      controller.isRevealed = false;
    }),
    getState: () => ({
      get isRevealed() {
        return controller.isRevealed;
      },
      onFindReplaceStateChange: (l: (e: { isRevealed: boolean }) => void) => {
        listener = l;
        return { dispose: () => {} };
      },
    }),
    fireStateChange: (e: { isRevealed: boolean }) => listener?.(e),
  };
  return controller;
}

function makeFindableModified(controller: ReturnType<typeof makeFindController>) {
  return {
    getModel: () => ({ setEOL: vi.fn() }),
    ...scrollStubs(),
    getAction: (id: string) => (id === "actions.find" ? { run: controller.run } : undefined),
    getContribution: () => controller,
  };
}

// The modified editor's scroll API, used by the viewer to persist the reader's
// place in the diff.
function scrollStubs() {
  return {
    setScrollTop: vi.fn(),
    onDidScrollChange: vi.fn(() => ({ dispose: () => {} })),
    // The find effects call these on mount; the search-specific tests below
    // swap in a real controller. Undefined is a valid "no find widget open".
    getAction: () => undefined,
    getContribution: () => undefined,
  };
}

beforeEach(() => {
  h.diffProps = null;
  h.onMount = null;
  h.commentOptions = null;
});
afterEach(() => {
  cleanup();
  // writeCodeFontSizePx (live-apply test) persists to localStorage; clear it so
  // other suites start from the code-font default.
  localStorage.clear();
});

describe("MonacoDiffViewer", () => {
  it("feeds before→original and after→modified into the diff editor", async () => {
    renderDiff({ before: "old line\n", after: "new line\n", layout: "split" });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    // The diff must compare the server's before/after exactly — swapping these
    // would invert additions/deletions.
    expect(h.diffProps?.original).toBe("old line\n");
    expect(h.diffProps?.modified).toBe("new line\n");
    // The diff is never editable, regardless of permission.
    expect(h.diffProps?.options?.readOnly).toBe(true);
    // Long unchanged runs collapse into expandable bands (only changed hunks
    // + context are shown), matching the previous diff view.
    expect(h.diffProps?.options?.hideUnchangedRegions?.enabled).toBe(true);
    expect(h.diffProps?.options?.fontWeight).toBe("400");
  });

  it.each([
    { layout: "split" as const, sideBySide: true },
    { layout: "unified" as const, sideBySide: false },
  ])("maps layout=$layout to renderSideBySide=$sideBySide", async ({ layout, sideBySide }) => {
    renderDiff({ before: "a", after: "b", layout });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    expect(h.diffProps?.options?.renderSideBySide).toBe(sideBySide);
  });

  it.each([
    { wrapLines: true as const, diffWordWrap: "on" as const },
    { wrapLines: false as const, diffWordWrap: "off" as const },
  ])(
    "maps wrapLines=$wrapLines to diffWordWrap=$diffWordWrap",
    async ({ wrapLines, diffWordWrap }) => {
      renderDiff({ before: "a", after: "b", layout: "unified", wrapLines });
      await waitFor(() => expect(h.diffProps).not.toBeNull());
      expect(h.diffProps?.options?.diffWordWrap).toBe(diffWordWrap);
    },
  );

  it.each([{ hideWhitespace: true as const }, { hideWhitespace: false as const }])(
    "maps hideWhitespace=$hideWhitespace to ignoreTrimWhitespace",
    async ({ hideWhitespace }) => {
      renderDiff({ before: "a", after: "b", layout: "unified", hideWhitespace });
      await waitFor(() => expect(h.diffProps).not.toBeNull());
      expect(h.diffProps?.options?.ignoreTrimWhitespace).toBe(hideWhitespace);
    },
  );

  it("treats a null side (new/deleted file) as empty content", async () => {
    renderDiff({ before: null, after: "created\n", layout: "unified" });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    // before=null → new file: original must be "" so the whole file shows as added.
    expect(h.diffProps?.original).toBe("");
    expect(h.diffProps?.modified).toBe("created\n");
  });

  it("wires getModifiedEditor() into the comment layer on mount", async () => {
    const setEOL = vi.fn();
    const fakeModified = { getModel: () => ({ setEOL }), ...scrollStubs() };
    renderDiff({ before: "a", after: "b\r\n", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    // h.onMount is MonacoDiffViewer's real handleMount (captured from the
    // DiffEditor onMount prop), so invoking it runs the actual
    // getModifiedEditor() → modifiedEditorRef wiring — not a mock echo.
    act(() => {
      h.onMount?.(
        {
          getModifiedEditor: () => fakeModified,
          getOriginalEditor: () => ({ getModel: () => null }),
        } as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    // The modified editor is handed to the comment hook and `mounted` flips, so
    // its listeners/decorations wire up. A regression here = comments silently
    // stop working in the diff.
    expect(h.commentOptions?.editorRef.current).toBe(fakeModified);
    expect(h.commentOptions?.mounted).toBe(true);
    // CRLF "after" → model EOL set to CRLF (1) so comment offsets stay aligned.
    expect(setEOL).toHaveBeenCalledWith(1);
  });

  it("restores and records the modified side's scroll offset", async () => {
    saveScrollTop("viewer-diff:conv_1:src/a.ts", 260);
    const setScrollTop = vi.fn();
    const onDidScrollChange = vi.fn((_listener: (e: { scrollTop: number }) => void) => ({
      dispose: () => {},
    }));
    const fakeModified = {
      getModel: () => ({ setEOL: vi.fn() }),
      setScrollTop,
      onDidScrollChange,
      getDomNode: () => document.createElement("div"),
      getAction: () => undefined,
      getContribution: () => undefined,
    };
    renderDiff({ before: "a", after: "b", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    act(() => {
      h.onMount?.(
        {
          getModifiedEditor: () => fakeModified,
          getOriginalEditor: () => ({ getModel: () => null }),
        } as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    // The reader's place in the diff is restored, and further scrolling is
    // cached under the diff's own key.
    expect(setScrollTop).toHaveBeenCalledWith(260);
    const handler = onDidScrollChange.mock.calls[0]![0];
    handler({ scrollTop: 260 });
    handler({ scrollTop: 88 });
    expect(getSavedScrollTop("viewer-diff:conv_1:src/a.ts")).toBe(88);
  });

  it("does not let the mount-time clamp overwrite the diff's saved offset", async () => {
    saveScrollTop("viewer-diff:conv_1:src/a.ts", 260);
    const setScrollTop = vi.fn();
    const onDidScrollChange = vi.fn((_listener: (e: { scrollTop: number }) => void) => ({
      dispose: () => {},
    }));
    const fakeModified = {
      getModel: () => ({ setEOL: vi.fn() }),
      setScrollTop,
      onDidScrollChange,
      getDomNode: () => document.createElement("div"),
      getAction: () => undefined,
      getContribution: () => undefined,
    };
    renderDiff({ before: "a", after: "b", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    act(() => {
      h.onMount?.(
        {
          getModifiedEditor: () => fakeModified,
          getOriginalEditor: () => ({ getModel: () => null }),
        } as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });
    setScrollTop.mockClear();

    // The panes aren't laid out yet, so Monaco reports the clamped 0; caching it
    // would lose the reader's place, so the target is re-asserted instead.
    const handler = onDidScrollChange.mock.calls[0]![0];
    handler({ scrollTop: 0 });
    expect(getSavedScrollTop("viewer-diff:conv_1:src/a.ts")).toBe(260);
    expect(setScrollTop).toHaveBeenCalledWith(260);

    // Once the offset is reachable, saving resumes.
    handler({ scrollTop: 260 });
    handler({ scrollTop: 12 });
    expect(getSavedScrollTop("viewer-diff:conv_1:src/a.ts")).toBe(12);
  });

  it("re-fonts the mounted diff editor when the code-font preference changes", async () => {
    const updateOptions = vi.fn();
    const fakeModified = { getModel: () => ({ setEOL: vi.fn() }), ...scrollStubs() };
    const fakeDiff = {
      getModifiedEditor: () => fakeModified,
      getOriginalEditor: () => ({ getModel: () => null }),
      updateOptions,
    };
    renderDiff({ before: "a", after: "b", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    // Mount wires diffEditorRef → our fake diff editor.
    act(() => {
      h.onMount?.(
        fakeDiff as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    // A Settings change emits through the code-font pub/sub; the mounted diff
    // editor re-fonts in place via updateOptions (both panes) — the imperative
    // path a fixed-pixel Monaco widget needs, vs the chrome font's CSS variable.
    act(() => {
      writeCodeFontSizePx(20);
    });
    expect(updateOptions).toHaveBeenCalledWith({
      fontSize: 20,
      fontFamily: codeFontFamilyForEditor(""),
      fontWeight: "400",
    });
  });

  it("runs Monaco's find action on the modified side when searchOpen is set", async () => {
    const controller = makeFindController();
    const fakeModified = makeFindableModified(controller);
    renderDiff({ before: "a", after: "b", layout: "split", searchOpen: true });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    act(() => {
      h.onMount?.(
        {
          getModifiedEditor: () => fakeModified,
          getOriginalEditor: () => ({ getModel: () => null }),
        } as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    // searchOpen=true opens Monaco's native find on the modified editor — the
    // path Cmd+F drives in the managed embed, where the keybinding won't fire.
    expect(controller.run).toHaveBeenCalledTimes(1);
    expect(controller.closeFindWidget).not.toHaveBeenCalled();
  });

  it("reports back via onSearchHandled when find is closed from within Monaco", async () => {
    const controller = makeFindController();
    const fakeModified = makeFindableModified(controller);
    const onSearchHandled = vi.fn();
    renderDiff({ before: "a", after: "b", layout: "split", searchOpen: true, onSearchHandled });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    act(() => {
      h.onMount?.(
        {
          getModifiedEditor: () => fakeModified,
          getOriginalEditor: () => ({ getModel: () => null }),
        } as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    // Simulate Escape / the widget's ✕: Monaco flips isRevealed false and fires
    // a change whose isRevealed flag marks that field as changed.
    controller.isRevealed = false;
    act(() => {
      controller.fireStateChange({ isRevealed: true });
    });
    expect(onSearchHandled).toHaveBeenCalledTimes(1);
  });

  it("resets the widget model and disposes both text models on unmount", async () => {
    // @monaco-editor/react disposes the models before the diff widget, which the
    // bundled Monaco rejects. We take disposal over (keepCurrent*) and tear down
    // in the safe order: detach the widget's model, then dispose both models.
    const originalModel = { dispose: vi.fn() };
    const modifiedModel = { dispose: vi.fn(), setEOL: vi.fn() };
    const setModel = vi.fn();
    const fakeModified = {
      getModel: () => modifiedModel,
      ...scrollStubs(),
    };
    const fakeDiff = {
      getModifiedEditor: () => fakeModified,
      getOriginalEditor: () => ({ getModel: () => originalModel }),
      setModel,
    };
    const { unmount } = renderDiff({ before: "a", after: "b", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());
    act(() => {
      h.onMount?.(
        fakeDiff as unknown as Parameters<DiffOnMount>[0],
        {
          editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
        } as unknown as Parameters<DiffOnMount>[1],
      );
    });

    act(() => unmount());

    // The widget's model is detached before the models are disposed, so a model
    // is never disposed while still attached to the diff widget.
    expect(setModel).toHaveBeenCalledWith(null);
    expect(originalModel.dispose).toHaveBeenCalledTimes(1);
    expect(modifiedModel.dispose).toHaveBeenCalledTimes(1);
  });

  it("passes keepCurrent* so the library does not dispose the models itself", async () => {
    renderDiff({ before: "a", after: "b", layout: "split" });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    // Without these, @monaco-editor/react disposes the text models before the
    // diff widget on unmount and Monaco throws.
    const props = h.diffProps as {
      keepCurrentOriginalModel?: boolean;
      keepCurrentModifiedModel?: boolean;
    };
    expect(props.keepCurrentOriginalModel).toBe(true);
    expect(props.keepCurrentModifiedModel).toBe(true);
  });
});

describe("diff file position navigation", () => {
  function navigationEditors() {
    const container = document.createElement("div");
    const layouts = new Set<() => void>();
    const updates = new Set<() => void>();
    let computed = false;
    const modified = {
      ...scrollStubs(),
      getModel: () => ({
        setEOL: vi.fn(),
        validatePosition: (p: { lineNumber: number; column: number }) => p,
      }),
      getLayoutInfo: () => ({ width: 600, height: 800 }),
      getDomNode: () => container,
      setPosition: vi.fn(),
      revealPositionInCenter: vi.fn(),
      onDidLayoutChange: (fn: () => void) => {
        layouts.add(fn);
        return {
          dispose: () => {
            layouts.delete(fn);
          },
        };
      },
    };
    const original = {
      getModel: () => null,
      setPosition: vi.fn(),
      revealPositionInCenter: vi.fn(),
    };
    const diff = {
      getOriginalEditor: () => original,
      getModifiedEditor: () => modified,
      getContainerDomNode: () => container,
      getLineChanges: () => (computed ? [] : null),
      onDidUpdateDiff: (fn: () => void) => {
        updates.add(fn);
        return {
          dispose: () => {
            updates.delete(fn);
          },
        };
      },
    };
    return {
      modified,
      original,
      container,
      diff,
      finishDiff: () => {
        computed = true;
        for (const fn of updates) fn();
      },
      resize: () => {
        for (const fn of layouts) fn();
      },
    };
  }

  it.each(["split", "unified"] as const)(
    "centers repeated citations and stops after a handled wheel event in %s mode",
    async (layout) => {
      const editors = navigationEditors();
      const props = { before: "old", after: "new", layout, position: { line: 40, column: 7 } };
      const { rerender, unmount } = renderDiff(props);
      await waitFor(() => expect(h.onMount).not.toBeNull());
      act(() =>
        h.onMount?.(
          editors.diff as unknown as Parameters<DiffOnMount>[0],
          {
            editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
          } as unknown as Parameters<DiffOnMount>[1],
        ),
      );
      expect(h.diffProps?.options?.scrollBeyondLastLine).toBe(true);
      expect(editors.modified.revealPositionInCenter).not.toHaveBeenCalled();
      act(editors.finishDiff);
      await waitFor(() =>
        expect(editors.modified.revealPositionInCenter).toHaveBeenLastCalledWith(
          { lineNumber: 40, column: 7 },
          1,
        ),
      );
      expect(editors.original.setPosition).not.toHaveBeenCalled();
      expect(editors.original.revealPositionInCenter).not.toHaveBeenCalled();
      rerender(diffTree({ ...props, position: { line: 115 } }));
      expect(editors.modified.revealPositionInCenter).toHaveBeenLastCalledWith(
        { lineNumber: 115, column: 1 },
        1,
      );
      rerender(diffTree({ ...props, position: { line: 115 } }));
      expect(editors.modified.revealPositionInCenter).toHaveBeenCalledTimes(3);
      act(editors.resize);
      expect(editors.modified.revealPositionInCenter).toHaveBeenCalledTimes(4);
      const childEditor = document.createElement("div");
      editors.container.append(childEditor);
      // Monaco consumes handled wheel events inside the child editor.
      childEditor.addEventListener("wheel", (event) => event.stopPropagation());
      vi.useFakeTimers();
      try {
        act(editors.finishDiff);
        childEditor.dispatchEvent(new WheelEvent("wheel", { bubbles: true, deltaY: 100 }));
        act(editors.resize);
        act(editors.finishDiff);
        act(() => vi.advanceTimersToNextFrame());
        expect(editors.modified.revealPositionInCenter).toHaveBeenCalledTimes(4);
      } finally {
        vi.useRealTimers();
      }
      unmount();
    },
  );

  it.each(["interaction", "unmount"] as const)(
    "cancels a queued diff jump on %s",
    async (reason) => {
      const editors = navigationEditors();
      const { unmount } = renderDiff({
        before: "old",
        after: "new",
        layout: "split",
        position: { line: 100 },
      });
      await waitFor(() => expect(h.onMount).not.toBeNull());
      act(() =>
        h.onMount?.(
          editors.diff as unknown as Parameters<DiffOnMount>[0],
          {
            editor: { EndOfLineSequence: { LF: 0, CRLF: 1 } },
          } as unknown as Parameters<DiffOnMount>[1],
        ),
      );
      vi.useFakeTimers();
      try {
        act(editors.finishDiff);
        if (reason === "unmount") unmount();
        else editors.container.dispatchEvent(new Event("pointerdown"));
        act(() => vi.advanceTimersToNextFrame());
        expect(editors.modified.setPosition).not.toHaveBeenCalled();
        expect(editors.modified.revealPositionInCenter).not.toHaveBeenCalled();
      } finally {
        vi.useRealTimers();
      }
    },
  );
});
