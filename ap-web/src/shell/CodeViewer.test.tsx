import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useFileContent } from "@/hooks/useFileContent";
import { CodeViewer } from "./CodeViewer";

// ── Module mocks ──────────────────────────────────────────────────────────────

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));
// Stub Shiki so the highlighting effect never fires an async callback that
// would mutate state after the test cleans up.
vi.mock("@/components/ai-elements/code-block", () => ({ highlightCode: vi.fn(() => null) }));
vi.mock("./MarkdownRichTextViewer", () => ({ MarkdownRichTextViewer: () => null }));
// Stub the lazy Monaco editor so the heavy monaco-editor bundle isn't loaded in
// jsdom; its presence in the DOM is the signal that a file was routed to Monaco.
vi.mock("./MonacoCodeEditor", () => ({
  MonacoCodeEditor: () => <div data-testid="monaco-editor-stub" />,
}));

import * as permissions from "@/hooks/usePermissions";

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeFileQuery(content: string, truncated = false): ReturnType<typeof useFileContent> {
  return {
    data: { content, encoding: "utf-8", truncated },
    isLoading: false,
    isError: false,
    isSuccess: true,
    error: null,
  } as unknown as ReturnType<typeof useFileContent>;
}

const noopRef = { current: null };

function renderViewer(
  content: string,
  panelOpen = true,
  path = "notes.md",
  opts: { viewMode?: "editor" | "preview" | "source" | "diff"; truncated?: boolean } = {},
) {
  // Markdown source view still renders via the Shiki DOM, where the
  // select-all/copy override under test lives. Non-markdown files now render in
  // Monaco, which handles select-all + copy natively, so this suite defaults to
  // a .md path to exercise the remaining Shiki path.
  return render(
    <CodeViewer
      conversationId="conv_1"
      path={path}
      fileQuery={makeFileQuery(content, opts.truncated)}
      comments={[]}
      activeSelection={null}
      onSetActiveSelection={() => {}}
      panelOpen={panelOpen}
      searchOpen={false}
      setSearchOpen={() => {}}
      searchInputRef={noopRef}
      viewMode={opts.viewMode ?? "source"}
    />,
  );
}

/**
 * Dispatches a `copy` event to `document` with a mock clipboardData.
 * Returns the `setData` spy so the caller can assert on what was written.
 *
 * Uses a plain `Event` rather than `new ClipboardEvent(...)` because jsdom
 * does not expose `ClipboardEvent` as a global constructor.
 */
function fireCopyEvent(): ReturnType<typeof vi.fn> {
  const setData = vi.fn();
  const event = new Event("copy", { bubbles: true, cancelable: true });
  Object.defineProperty(event, "clipboardData", {
    value: { setData, getData: vi.fn() },
    writable: false,
  });
  document.dispatchEvent(event);
  return setData;
}

// ── Setup / teardown ──────────────────────────────────────────────────────────

beforeEach(() => {
  vi.mocked(permissions.useCanEdit).mockReturnValue(true);
});

afterEach(() => {
  cleanup();
});

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("CodeViewer Cmd+A select-all and copy interception", () => {
  it("copy after Cmd+A writes raw file content to clipboardData", () => {
    const content = "const x = 1;\nconst y = 2;\nconst z = 3;";
    renderViewer(content);

    fireEvent.keyDown(window, { key: "a", metaKey: true });
    const setData = fireCopyEvent();

    // The raw file string must land in clipboardData unchanged so the user
    // gets the original source — not the DOM-serialized text which omits
    // newlines between flex-layout line rows.
    expect(setData).toHaveBeenCalledWith("text/plain", content);
  });

  it("copy after Ctrl+A writes raw file content to clipboardData", () => {
    const content = "line1\nline2";
    renderViewer(content);

    // ctrlKey is the non-Mac equivalent; must behave identically to metaKey.
    fireEvent.keyDown(window, { key: "a", ctrlKey: true });
    const setData = fireCopyEvent();

    expect(setData).toHaveBeenCalledWith("text/plain", content);
  });

  it("preserves all embedded newlines in multiline content", () => {
    // Primary regression guard: the old DOM-copy path squashed flex-row div
    // boundaries and delivered concatenated lines without any \n separators.
    const content = "function foo() {\n  return 42;\n}\n";
    renderViewer(content);

    fireEvent.keyDown(window, { key: "a", metaKey: true });
    const setData = fireCopyEvent();

    expect(setData).toHaveBeenCalledWith("text/plain", content);
  });

  it("copy without prior Cmd+A is not intercepted", () => {
    renderViewer("line1\nline2");

    // No Cmd+A fired — the pending flag is never set; browser default applies.
    const setData = fireCopyEvent();

    // setData must not be called because the handler only overrides clipboard
    // content after the user explicitly selected-all via Cmd+A.
    expect(setData).not.toHaveBeenCalled();
  });

  it("mousedown between Cmd+A and copy clears the pending flag", () => {
    renderViewer("line1\nline2");

    fireEvent.keyDown(window, { key: "a", metaKey: true });
    // Mousedown (e.g. user repositions the pointer after select-all) must
    // clear the flag so the subsequent copy is not treated as a select-all copy.
    // Use document.body rather than document itself: the dismiss handler calls
    // e.target.closest(...) which is not defined on the Document node.
    fireEvent.mouseDown(document.body);
    const setData = fireCopyEvent();

    // Flag was cleared by mousedown — the copy handler must not write to
    // clipboardData; the browser default handles the (now partial) selection.
    expect(setData).not.toHaveBeenCalled();
  });

  it("Cmd+A does not intercept copy when an input element has focus", () => {
    renderViewer("line1\nline2");

    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    fireEvent.keyDown(window, { key: "a", metaKey: true });
    const setData = fireCopyEvent();

    // Input-focused Cmd+A must be passed through so the input's native
    // select-all behaviour is preserved; our flag must not be set.
    expect(setData).not.toHaveBeenCalled();

    document.body.removeChild(input);
  });

  it("Cmd+A does not intercept copy when panelOpen is false", () => {
    // The keyboard handler is only registered when panelOpen=true; firing
    // Cmd+A while the panel is closed must have no effect.
    renderViewer("line1\nline2", false);

    fireEvent.keyDown(window, { key: "a", metaKey: true });
    const setData = fireCopyEvent();

    expect(setData).not.toHaveBeenCalled();
  });
});

describe("CodeViewer editor routing", () => {
  it("routes non-markdown files to the Monaco editor", async () => {
    renderViewer("const x = 1;", true, "src/index.ts");
    // The lazy Monaco stub mounting proves a .ts file is routed to Monaco
    // rather than the Shiki line-by-line DOM render. findByTestId awaits the
    // Suspense boundary resolving the lazy import.
    expect(await screen.findByTestId("monaco-editor-stub")).toBeDefined();
  });

  it("keeps markdown source on the Shiki path (not Monaco)", () => {
    renderViewer("# heading", true, "notes.md");
    // Markdown source must NOT route to Monaco — it stays on the Shiki render
    // (TipTap handles markdown editing; Monaco is for non-markdown files).
    expect(screen.queryByTestId("monaco-editor-stub")).toBeNull();
  });
});

describe("CodeViewer truncated preview", () => {
  it("shows the truncated banner in markdown preview mode", () => {
    renderViewer("# big file", true, "notes.md", { viewMode: "preview", truncated: true });
    // Preview renders incomplete content when the file is truncated; the banner
    // is the only in-UI signal, so it must appear in preview too — not just the
    // editor/source surfaces.
    expect(screen.getByText(/too large to load fully/)).toBeDefined();
  });

  it("shows no banner in markdown preview when not truncated", () => {
    renderViewer("# full file", true, "notes.md", { viewMode: "preview", truncated: false });
    expect(screen.queryByText(/too large to load fully/)).toBeNull();
  });
});

describe("CodeViewer HTML preview link handling", () => {
  // Regression for #777: links in a rendered HTML artifact did nothing on
  // click because the preview iframe had sandbox="" (no popups) and links
  // carried no target. The preview must inject a new-tab default and allow
  // popups, while still withholding scripts and same-origin.
  it("injects a new-tab base default and a popup-allowing sandbox", () => {
    const { container } = renderViewer(
      '<html><head></head><body><a href="https://example.com">x</a></body></html>',
      true,
      "report.html",
      { viewMode: "preview" },
    );
    const iframe = container.querySelector('iframe[title="HTML preview"]');
    expect(iframe).not.toBeNull();
    expect(iframe!.getAttribute("srcdoc")).toContain('<base target="_blank">');
    const sandbox = iframe!.getAttribute("sandbox") ?? "";
    expect(sandbox).toContain("allow-popups");
    expect(sandbox).not.toContain("allow-scripts");
    expect(sandbox).not.toContain("allow-same-origin");
  });
});
