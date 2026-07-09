// Tests for MonacoCodeEditor.
//
// Two layers:
//   1. buildCommentDecorations — the comment offset→range bridge, tested as a
//      pure function against a deterministic fake model. This is where the
//      character-offset math lives, so it gets focused unit coverage.
//   2. The component's permission/truncation gating — Monaco can't mount in
//      jsdom, so @monaco-editor/react's Editor is mocked to capture the props
//      it receives. We assert on the *actual* `readOnly` value passed to the
//      editor (real behavior), so the P5 truncated guard turns red if it
//      regresses.

import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Comment } from "@/hooks/useComments";
import type { ActiveSelection, SaveStatus } from "./codeViewerHelpers";

// ── Module mocks ──────────────────────────────────────────────────────────────

// Capture the props the editor is rendered with (it returns null — we only
// need the props, not a real DOM editor).
const h = vi.hoisted(() => ({
  editorProps: null as {
    options?: { readOnly?: boolean };
    theme?: string;
    language?: string;
  } | null,
}));
vi.mock("@monaco-editor/react", () => ({
  Editor: (props: { options?: { readOnly?: boolean }; theme?: string; language?: string }) => {
    h.editorProps = props;
    return null;
  },
}));

// Stub the Monaco setup so the real (heavy, side-effectful) monaco-editor and
// shiki bundles are never loaded in jsdom. Resolved promises let the inner
// component flip `ready` and render the editor.
vi.mock("./monacoSetup", () => ({
  ensureMonacoReady: vi.fn(() => Promise.resolve()),
  ensureLanguage: vi.fn(() => Promise.resolve()),
  monacoLanguageId: vi.fn((lang: string) => lang),
  resolvedThemeToMonaco: vi.fn(() => "github-light"),
}));

vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "light" }) }));
vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));
vi.mock("./useMarkdownEditorSync", () => ({ useMarkdownEditorSync: vi.fn() }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));

import * as permissions from "@/hooks/usePermissions";
import * as syncHook from "./useMarkdownEditorSync";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";
import * as monacoSetup from "./monacoSetup";
import { MonacoCodeEditor } from "./MonacoCodeEditor";
import { buildCommentDecorations } from "./useMonacoCommentLayer";

// ── Helpers ───────────────────────────────────────────────────────────────────

function mkComment(overrides: Partial<Comment>): Comment {
  return {
    id: "c1",
    conversation_id: "conv_1",
    path: "/a.ts",
    start_index: 0,
    end_index: 0,
    body: "",
    status: "draft",
    created_at: 0,
    updated_at: 0,
    anchor_content: null,
    created_by: null,
    ...overrides,
  };
}

type DecoModel = Parameters<typeof buildCommentDecorations>[0];

/**
 * Fake text model with a faithful offset→position mapping (1-based line and
 * column, Monaco convention). A wrong offset→range computation in
 * buildCommentDecorations therefore produces a wrong, detectable range.
 */
function fakeModel(content: string): DecoModel {
  return {
    getPositionAt(offset: number) {
      const before = content.slice(0, offset);
      const lines = before.split("\n");
      return { lineNumber: lines.length, column: lines[lines.length - 1].length + 1 };
    },
  } as unknown as DecoModel;
}

// "abc\ndefgh\nij": indices a0 b1 c2 \n3 d4 e5 f6 g7 h8 \n9 i10 j11
const CONTENT = "abc\ndefgh\nij";

function setupHooks(
  overrides: {
    canEdit?: boolean;
    isDirty?: boolean;
    // Write-mutation state — the save-status chip is derived from these.
    isPending?: boolean;
    isError?: boolean;
    isSuccess?: boolean;
    // undefined = unknown (treated as online); false = offline.
    runnerOnline?: boolean;
  } = {},
) {
  vi.mocked(permissions.useCanEdit).mockReturnValue(overrides.canEdit ?? true);
  vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue({
    editorKey: 1,
    isDirty: overrides.isDirty ?? false,
    setDirty: vi.fn(),
    hasExternalUpdate: false,
    discardAndApplyExternal: vi.fn(),
    dismissExternalUpdate: vi.fn(),
    markSaved: vi.fn(),
    reconcileServerContent: vi.fn().mockReturnValue(false),
  });
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: overrides.isPending ?? false,
    isError: overrides.isError ?? false,
    isSuccess: overrides.isSuccess ?? false,
    reset: vi.fn(),
    mutateAsync: vi.fn(),
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(overrides.runnerOnline);
}

function renderEditor(
  props: { truncated?: boolean; onSaveStatusChange?: (s: SaveStatus) => void } = {},
) {
  return render(
    <MonacoCodeEditor
      content="const x = 1;"
      conversationId="conv_1"
      path="src/a.ts"
      isSettled={true}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      {...props}
    />,
  );
}

beforeEach(() => {
  h.editorProps = null;
  setupHooks();
});

afterEach(() => {
  cleanup();
});

// ── buildCommentDecorations (offset → range bridge) ─────────────────────────────

describe("buildCommentDecorations", () => {
  it("maps a comment's offsets to a 1-based line/column range with the dim class", () => {
    const decos = buildCommentDecorations(
      fakeModel(CONTENT),
      [mkComment({ start_index: 1, end_index: 3 })],
      null,
    );

    // One decoration for the one comment. If 0, the comment was dropped; if 2,
    // an active-selection decoration leaked in despite activeSelection === null.
    expect(decos).toHaveLength(1);
    // "bc" on line 1 → cols 2..4. A wrong offset→position computation (or
    // swapped start/end) would yield a different range here.
    expect(decos[0].range).toEqual({
      startLineNumber: 1,
      startColumn: 2,
      endLineNumber: 1,
      endColumn: 4,
    });
    // Non-active saved comment uses the dim class.
    expect(decos[0].options.inlineClassName).toBe("oa-comment");
  });

  it("uses the active class when the active selection matches the comment", () => {
    const active: ActiveSelection = { start_index: 1, end_index: 3, anchor_content: "bc" };
    const decos = buildCommentDecorations(
      fakeModel(CONTENT),
      [mkComment({ start_index: 1, end_index: 3 })],
      active,
    );

    // Still one decoration (the active selection coincides with the comment, so
    // no separate pending highlight is added).
    expect(decos).toHaveLength(1);
    // The matching comment is promoted to the active (stronger) class — this is
    // what visually marks the focused comment.
    expect(decos[0].options.inlineClassName).toBe("oa-comment-active");
  });

  it("adds a separate active highlight for a not-yet-saved selection", () => {
    const active: ActiveSelection = { start_index: 5, end_index: 7, anchor_content: "fg" };
    const decos = buildCommentDecorations(
      fakeModel(CONTENT),
      [mkComment({ start_index: 1, end_index: 3 })],
      active,
    );

    // 2 = the saved comment (dim) + the pending selection (active). If 1, the
    // pending-selection branch failed to add the highlight the user sees while
    // composing a comment.
    expect(decos).toHaveLength(2);
    const classes = decos.map((d) => d.options.inlineClassName);
    expect(classes).toContain("oa-comment");
    expect(classes).toContain("oa-comment-active");
    // The pending highlight maps offsets 5..7 → line 2, cols 2..4.
    const pending = decos.find((d) => d.options.inlineClassName === "oa-comment-active");
    expect(pending?.range).toEqual({
      startLineNumber: 2,
      startColumn: 2,
      endLineNumber: 2,
      endColumn: 4,
    });
  });

  it("does not add a highlight for an empty (collapsed) active selection", () => {
    const active: ActiveSelection = { start_index: 5, end_index: 5, anchor_content: "" };
    const decos = buildCommentDecorations(
      fakeModel(CONTENT),
      [mkComment({ start_index: 1, end_index: 3 })],
      active,
    );

    // Only the saved comment. A collapsed selection (caret, no range) must not
    // produce a zero-width highlight.
    expect(decos).toHaveLength(1);
    expect(decos[0].options.inlineClassName).toBe("oa-comment");
  });
});

// ── Permission / truncation gating ──────────────────────────────────────────────

describe("MonacoCodeEditor read-only / truncated gating", () => {
  it.each([
    {
      name: "editable when permitted and not truncated",
      canEdit: true,
      truncated: false,
      readOnly: false,
      banner: false,
    },
    {
      name: "read-only without edit permission",
      canEdit: false,
      truncated: false,
      readOnly: true,
      banner: false,
    },
    {
      name: "read-only + banner when truncated, even with permission",
      canEdit: true,
      truncated: true,
      readOnly: true,
      banner: true,
    },
  ])("$name", async ({ canEdit, truncated, readOnly, banner }) => {
    setupHooks({ canEdit });
    renderEditor({ truncated });

    // Wait for the ready effect to resolve and render the (mocked) editor.
    await waitFor(() => expect(h.editorProps).not.toBeNull());

    // The actual readOnly option handed to Monaco — real behavior. A regression
    // in the `canEdit && !truncated` guard flips this value.
    expect(h.editorProps?.options?.readOnly).toBe(readOnly);

    // The truncation banner appears only for truncated files; its presence is
    // what tells the user editing is disabled to prevent data loss.
    if (banner) expect(screen.getByText(/too large to load fully/)).toBeDefined();
    else expect(screen.queryByText(/too large to load fully/)).toBeNull();
  });
});

describe("MonacoCodeEditor save-status reporting", () => {
  // The editor has no Save button; it reports its auto-save lifecycle up to
  // FileViewer via onSaveStatusChange (which renders the toolbar chip). These
  // pin the derivation from the write mutation + dirty/offline state, including
  // its priority order (error > saving > offline > saved).
  it.each([
    {
      name: "unsaved while typing (dirty, online, pre-write)",
      state: { isDirty: true, runnerOnline: true },
      expected: "unsaved",
    },
    {
      name: "saving while a write is in flight",
      state: { isPending: true, isDirty: true, runnerOnline: true },
      expected: "saving",
    },
    {
      name: "saved once a clean write settles",
      state: { isSuccess: true, isDirty: false, runnerOnline: true },
      expected: "saved",
    },
    {
      name: "error when the last write failed",
      state: { isError: true, isDirty: true, runnerOnline: true },
      expected: "error",
    },
    {
      name: "offline when dirty and the runner is down",
      state: { isDirty: true, runnerOnline: false },
      expected: "offline",
    },
    {
      name: "error wins over a pending retry",
      state: { isError: true, isPending: true, isDirty: true, runnerOnline: true },
      expected: "error",
    },
    // A failed write leaves isError stuck on the mutation; once the user reverts
    // to a clean buffer there's nothing left to save, so the stale "Save failed"
    // chip must clear rather than linger.
    {
      name: "error clears when the buffer is reverted clean",
      state: { isError: true, isDirty: false, runnerOnline: true },
      expected: "idle",
    },
    // Dirty is resolved before "saved": a stale isSuccess from the prior save
    // must not mask the new edit while the next debounce is still pending.
    {
      name: "unsaved wins over a stale prior-save success",
      state: { isSuccess: true, isDirty: true, runnerOnline: true },
      expected: "unsaved",
    },
  ])("$name", async ({ state, expected }) => {
    const statuses: SaveStatus[] = [];
    setupHooks({ canEdit: true, ...state });
    renderEditor({ onSaveStatusChange: (s) => statuses.push(s) });
    // Flush the mount effects (the status effect reports on first commit).
    await act(async () => {});

    // Exactly one report, with the expected value. Asserting the whole array
    // (not just the last entry) pins both the derivation — a wrong priority
    // order yields a different value — and that the effect doesn't double-fire.
    expect(statuses).toEqual([expected]);
  });

  it("clears the transient 'saved' badge back to idle after the timeout", async () => {
    vi.useFakeTimers();
    try {
      const statuses: SaveStatus[] = [];
      setupHooks({ canEdit: true, isSuccess: true, isDirty: false, runnerOnline: true });
      renderEditor({ onSaveStatusChange: (s) => statuses.push(s) });
      await act(async () => {});
      // Only "saved" so far — the timer hasn't fired yet.
      expect(statuses).toEqual(["saved"]);

      // SAVED_BADGE_MS (2000) elapses → the chip self-clears so it doesn't
      // linger after the write landed. Asserting the full sequence proves the
      // setTimeout→idle actually fired (a missing clear leaves it ["saved"]).
      await act(async () => {
        vi.advanceTimersByTime(2000);
      });
      expect(statuses).toEqual(["saved", "idle"]);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("MonacoCodeEditor load failure", () => {
  it("surfaces an error instead of an infinite spinner when Monaco fails to load", async () => {
    setupHooks({ canEdit: true });
    vi.mocked(monacoSetup.ensureMonacoReady).mockRejectedValueOnce(new Error("init failed"));

    renderEditor();

    // A rejected init must show an error (and not leave the user stuck on
    // "Loading…" with an unhandled promise rejection).
    expect(await screen.findByText(/failed to load the editor/i)).toBeDefined();
    expect(screen.queryByText("Loading…")).toBeNull();
  });
});
