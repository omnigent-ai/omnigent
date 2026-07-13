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
      readOnly?: boolean;
      hideUnchangedRegions?: { enabled?: boolean };
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

function renderDiff(props: {
  before: string | null;
  after: string | null;
  layout: "unified" | "split";
  hideWhitespace?: boolean;
}) {
  return render(
    <MonacoDiffViewer
      before={props.before}
      after={props.after}
      path="src/a.ts"
      layout={props.layout}
      hideWhitespace={props.hideWhitespace ?? false}
      conversationId="conv_1"
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />,
  );
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
  });

  it.each([
    { layout: "split" as const, sideBySide: true },
    { layout: "unified" as const, sideBySide: false },
  ])("maps layout=$layout to renderSideBySide=$sideBySide", async ({ layout, sideBySide }) => {
    renderDiff({ before: "a", after: "b", layout });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    expect(h.diffProps?.options?.renderSideBySide).toBe(sideBySide);
  });

  it("treats a null side (new/deleted file) as empty content", async () => {
    renderDiff({ before: null, after: "created\n", layout: "unified" });
    await waitFor(() => expect(h.diffProps).not.toBeNull());
    // before=null → new file: original must be "" so the whole file shows as added.
    expect(h.diffProps?.original).toBe("");
    expect(h.diffProps?.modified).toBe("created\n");
  });

  it("wires getModifiedEditor() into the comment layer on mount", async () => {
    const setEOL = vi.fn();
    const fakeModified = { getModel: () => ({ setEOL }) };
    renderDiff({ before: "a", after: "b\r\n", layout: "split" });
    await waitFor(() => expect(h.onMount).not.toBeNull());

    // h.onMount is MonacoDiffViewer's real handleMount (captured from the
    // DiffEditor onMount prop), so invoking it runs the actual
    // getModifiedEditor() → modifiedEditorRef wiring — not a mock echo.
    act(() => {
      h.onMount?.(
        { getModifiedEditor: () => fakeModified } as unknown as Parameters<DiffOnMount>[0],
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

  it("re-fonts the mounted diff editor when the code-font preference changes", async () => {
    const updateOptions = vi.fn();
    const fakeModified = { getModel: () => ({ setEOL: vi.fn() }) };
    const fakeDiff = { getModifiedEditor: () => fakeModified, updateOptions };
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
    });
  });
});
