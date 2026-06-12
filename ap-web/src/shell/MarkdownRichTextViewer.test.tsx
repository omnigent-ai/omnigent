// Tests for the read-only copy button in MarkdownRichTextViewer.
//
// MarkdownRichTextViewer has two modes controlled by `useCanEdit`:
//   - canEdit=true:  shows the MarkdownEditorToolbar (which has its own
//     copy button) and no overlay button.
//   - canEdit=false: shows a standalone "Copy" overlay button that
//     writes the raw `content` prop to the clipboard.
//
// These tests cover the read-only path. The TipTap editor and all
// comment/toolbar plugins are mocked to null so that jsdom doesn't need
// to exercise ProseMirror or markdown parsing APIs.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MarkdownRichTextViewer } from "./MarkdownRichTextViewer";

// ── Module mocks ──────────────────────────────────────────────────────────

// Stub TipTap so jsdom doesn't need a real ProseMirror DOM environment.
vi.mock("@tiptap/react", () => ({
  useEditor: vi.fn().mockReturnValue(null),
  EditorContent: () => null,
}));
vi.mock("@tiptap/markdown", () => ({
  Markdown: { configure: vi.fn().mockReturnValue({}) },
}));
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

// Null out sibling components.
vi.mock("./MarkdownCommentPlugin", () => ({ MarkdownCommentPlugin: () => null }));
vi.mock("./MarkdownEditorToolbar", () => ({ ToolbarPlugin: () => null }));

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));
vi.mock("./useMarkdownEditorSync", () => ({ useMarkdownEditorSync: vi.fn() }));
vi.mock("@/hooks/useWriteFileContent", () => ({ useWriteFileContent: vi.fn() }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));

import * as permissions from "@/hooks/usePermissions";
import * as syncHook from "./useMarkdownEditorSync";
import * as writeHook from "@/hooks/useWriteFileContent";
import * as runnerHook from "@/hooks/RunnerHealthProvider";

// ── Helpers ───────────────────────────────────────────────────────────────

function makeSyncResult(overrides: Partial<ReturnType<typeof syncHook.useMarkdownEditorSync>> = {}) {
  return {
    editorKey: 1,
    isDirty: false,
    setDirty: vi.fn(),
    hasExternalUpdate: false,
    discardAndApplyExternal: vi.fn(),
    dismissExternalUpdate: vi.fn(),
    markSaved: vi.fn(),
    reconcileServerContent: vi.fn().mockReturnValue(false),
    ...overrides,
  };
}

function setupReadOnlyHooks() {
  vi.mocked(permissions.useCanEdit).mockReturnValue(false);
  vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue(makeSyncResult());
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    reset: vi.fn(),
    mutateAsync: vi.fn(),
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(undefined);
}

function setupEditHooks(syncOverrides: Partial<ReturnType<typeof syncHook.useMarkdownEditorSync>> = {}) {
  vi.mocked(permissions.useCanEdit).mockReturnValue(true);
  vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue(makeSyncResult(syncOverrides));
  vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
    isPending: false,
    isError: false,
    reset: vi.fn(),
    mutateAsync: vi.fn(),
  } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
  vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(undefined);
}

function renderViewer(content: string, truncated = false) {
  return render(
    <MarkdownRichTextViewer
      content={content}
      conversationId="conv_1"
      path="/test.md"
      isSettled={true}
      truncated={truncated}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
    />,
  );
}

// ── Test suite ────────────────────────────────────────────────────────────

beforeEach(() => {
  setupReadOnlyHooks();
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
});

describe("MarkdownRichTextViewer read-only copy button", () => {
  it("renders a Copy button in read-only mode", () => {
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });

    renderViewer("# Hello");

    // Button must be present so the user can copy the document.
    expect(screen.getByTitle("Copy")).toBeDefined();
  });

  it("calls navigator.clipboard.writeText with the raw content when clicked", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    renderViewer("# Hello\n\nWorld");

    fireEvent.click(screen.getByTitle("Copy"));

    // The full raw markdown string must be written to the clipboard
    // unchanged so the recipient gets proper markdown.
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("# Hello\n\nWorld");
    });
  });

  it("shows 'Copied!' text immediately after a successful copy", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });

    renderViewer("content");

    fireEvent.click(screen.getByTitle("Copy"));

    // Visual confirmation that the copy succeeded — the label changes
    // to "Copied!" until the 2-second reset fires.
    await waitFor(() => {
      expect(screen.getByText("Copied!")).toBeDefined();
    });
  });

  it("does not render the Copy button in edit mode (toolbar handles it)", () => {
    // canEdit=true: the MarkdownEditorToolbar is shown instead, which
    // has its own copy button. The overlay button must not appear to
    // avoid duplicate copy controls.
    vi.mocked(permissions.useCanEdit).mockReturnValue(true);
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn() } });

    renderViewer("content");

    expect(screen.queryByTitle("Copy")).toBeNull();
  });
});

// ── Banner tests ───────────────────────────────────────────────────────────

describe("MarkdownRichTextViewer dirty banners", () => {
  it("shows the 'Unsaved changes' banner while dirty and online but not yet writing", () => {
    // runnerOnline undefined → saveDisabled false; isPending false (default
    // writeFile mock) → still in the debounce window, so the banner reads
    // "Unsaved changes —", not "Saving…".
    setupEditHooks({ isDirty: true, hasExternalUpdate: false });
    renderViewer("content");
    expect(screen.getByText(/Unsaved changes — commenting is available once saved/)).toBeDefined();
    expect(screen.queryByText(/Saving…/)).toBeNull();
    expect(screen.queryByText(/Runner offline/)).toBeNull();
    expect(screen.queryByText(/modified externally/)).toBeNull();
  });

  it("shows 'Saving…' in the banner once a write is in flight", () => {
    setupEditHooks({ isDirty: true, hasExternalUpdate: false });
    // Override the write hook to report an in-flight PUT.
    vi.mocked(writeHook.useWriteFileContent).mockReturnValue({
      isPending: true, isError: false, reset: vi.fn(), mutateAsync: vi.fn(),
    } as unknown as ReturnType<typeof writeHook.useWriteFileContent>);
    renderViewer("content");
    expect(screen.getByText(/Saving… commenting is available once saved/)).toBeDefined();
    expect(screen.queryByText(/Unsaved changes/)).toBeNull();
  });

  it("shows the offline banner when dirty and the runner is offline", () => {
    // saveDisabled = runnerOnline === false: autosave can't run, so the
    // banner explains edits will flush on reconnect rather than "Saving…".
    setupEditHooks({ isDirty: true, hasExternalUpdate: false });
    vi.mocked(runnerHook.useSessionRunnerOnline).mockReturnValue(false);
    renderViewer("content");
    expect(screen.getByText(/Runner offline/)).toBeDefined();
    expect(screen.queryByText(/Saving…/)).toBeNull();
  });

  it("shows external update banner instead when dirty and hasExternalUpdate", () => {
    setupEditHooks({ isDirty: true, hasExternalUpdate: true });
    renderViewer("content");
    expect(screen.getByText(/modified externally/)).toBeDefined();
    expect(screen.queryByText("Save your changes to enable commenting on selections.")).toBeNull();
  });

  it("shows 'Keep mine' and 'Load latest' buttons in the external update banner", () => {
    setupEditHooks({ isDirty: true, hasExternalUpdate: true });
    renderViewer("content");
    expect(screen.getByText("Keep mine")).toBeDefined();
    expect(screen.getByText("Load latest")).toBeDefined();
  });

  it("calls dismissExternalUpdate when 'Keep mine' is clicked", () => {
    const dismissExternalUpdate = vi.fn();
    setupEditHooks({ isDirty: true, hasExternalUpdate: true, dismissExternalUpdate });
    renderViewer("content");
    fireEvent.click(screen.getByText("Keep mine"));
    expect(dismissExternalUpdate).toHaveBeenCalledOnce();
  });

  it("calls discardAndApplyExternal when 'Load latest' is clicked", () => {
    const discardAndApplyExternal = vi.fn();
    setupEditHooks({ isDirty: true, hasExternalUpdate: true, discardAndApplyExternal });
    renderViewer("content");
    fireEvent.click(screen.getByText("Load latest"));
    expect(discardAndApplyExternal).toHaveBeenCalledOnce();
  });

  it("shows no banner when the editor is clean", () => {
    setupEditHooks({ isDirty: false, hasExternalUpdate: false });
    renderViewer("content");
    expect(screen.queryByText(/commenting is available once saved/)).toBeNull();
    expect(screen.queryByText(/Runner offline/)).toBeNull();
    expect(screen.queryByText(/modified externally/)).toBeNull();
  });

  it("shows no banner in read-only mode even when dirty", () => {
    // read-only viewers cannot edit, so banners about saving/commenting
    // are irrelevant and must not appear.
    vi.mocked(permissions.useCanEdit).mockReturnValue(false);
    vi.mocked(syncHook.useMarkdownEditorSync).mockReturnValue(
      makeSyncResult({ isDirty: true, hasExternalUpdate: true }),
    );
    renderViewer("content");
    expect(screen.queryByText(/modified externally/)).toBeNull();
    expect(screen.queryByText(/Runner offline/)).toBeNull();
    expect(screen.queryByText(/commenting is available once saved/)).toBeNull();
  });
});

// ── Truncated-file guard ─────────────────────────────────────────────────────

describe("MarkdownRichTextViewer truncated guard", () => {
  it("drops to read-only and shows a banner when the file is truncated", () => {
    // Even with edit permission, a truncated buffer must not be editable —
    // saving it would overwrite the unsent remainder of the file.
    setupEditHooks();
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn() } });

    renderViewer("# partial content", true);

    // Banner explains why editing is disabled.
    expect(screen.getByText(/too large to load fully/)).toBeDefined();
    // canEdit is forced false → the read-only Copy overlay appears instead of
    // the editing toolbar, proving the editor is no longer editable.
    expect(screen.getByTitle("Copy")).toBeDefined();
  });

  it("stays editable (no truncated banner) when not truncated", () => {
    setupEditHooks();
    vi.stubGlobal("navigator", { clipboard: { writeText: vi.fn() } });

    renderViewer("# full content", false);

    expect(screen.queryByText(/too large to load fully/)).toBeNull();
    // Edit mode → no read-only Copy overlay (toolbar handles copy).
    expect(screen.queryByTitle("Copy")).toBeNull();
  });
});
