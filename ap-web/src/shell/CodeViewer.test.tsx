import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useFileContent } from "@/hooks/useFileContent";
import { CodeViewer } from "./CodeViewer";
import { HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";

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

// A real (1×1, transparent) PNG, base64-encoded — i.e. exactly what the server
// returns for a binary file (encoding="base64", content_type="image/png").
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==";

function makeImageQuery(contentType: string, truncated = false): ReturnType<typeof useFileContent> {
  return {
    data: { content: PNG_BASE64, encoding: "base64", content_type: contentType, truncated },
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

describe("CodeViewer HTML preview sandbox", () => {
  // The HTML preview is the security-load-bearing surface: artifact content is
  // untrusted (agent/user-generated), so these assertions lock in the iframe's
  // isolation. A regression here (e.g. adding `allow-same-origin`) would let
  // artifact JS reach the host app's cookies, storage, and credentialed API.
  it("enables scripts but withholds same-origin, and forces links to a new tab", () => {
    const { container } = renderViewer(
      "<html><head></head><body><a href='https://example.com'>link</a></body></html>",
      true,
      "page.html",
      { viewMode: "preview" },
    );
    const iframe = container.querySelector('iframe[title="HTML preview"]');
    expect(iframe).not.toBeNull();
    const sandbox = iframe!.getAttribute("sandbox") ?? "";
    // Full-string lock: any change to the sandbox flags must be deliberate.
    expect(sandbox).toBe(HTML_PREVIEW_SANDBOX);
    // #778: scripts must run inside the preview.
    expect(sandbox).toContain("allow-scripts");
    // Security invariant: the artifact must never share the app's origin.
    expect(sandbox).not.toContain("allow-same-origin");
    // #777: every link opens in a new tab via the injected base tag.
    expect(iframe!.getAttribute("srcdoc")).toContain('<base target="_blank">');
  });
});

describe("CodeViewer image rendering", () => {
  // jsdom implements neither URL.createObjectURL nor revokeObjectURL; ImageViewer
  // calls both, so stub them and capture the blob it encodes.
  let createdBlob: Blob | null;

  beforeEach(() => {
    createdBlob = null;
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn((blob: Blob) => {
        createdBlob = blob;
        return "blob:mock-object-url";
      }),
      revokeObjectURL: vi.fn(),
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function renderImage(contentType: string, path = "logo.png", truncated = false) {
    return render(
      <CodeViewer
        conversationId="conv_1"
        path={path}
        fileQuery={makeImageQuery(contentType, truncated)}
        comments={[]}
        activeSelection={null}
        onSetActiveSelection={() => {}}
        panelOpen={true}
        searchOpen={false}
        setSearchOpen={() => {}}
        searchInputRef={noopRef}
        viewMode="source"
      />,
    );
  }

  it("renders a binary PNG as a blob-backed <img>, not source or placeholder", async () => {
    renderImage("image/png", "assets/logo.png");

    // alt is the basename; the src is the stubbed object URL — i.e. the image is
    // shown through a blob, never the base64 placeholder or Monaco/Shiki source.
    const img = (await screen.findByAltText("logo.png")) as HTMLImageElement;
    expect(img.getAttribute("src")).toBe("blob:mock-object-url");
    expect(screen.queryByTestId("monaco-editor-stub")).toBeNull();
    expect(screen.queryByText(/binary file/i)).toBeNull();

    // The blob handed to createObjectURL carries the server's MIME type and the
    // decoded PNG bytes (base64 round-trips through fileContentToBlob's atob path).
    expect(createdBlob?.type).toBe("image/png");
    expect(createdBlob?.size).toBe(atob(PNG_BASE64).length);
  });

  it("shows the truncated banner when a binary image was truncated", () => {
    renderImage("image/png", "logo.png", true);
    expect(screen.getByText(/too large to load fully/)).toBeDefined();
  });

  it("routes by content_type over extension (image MIME on a .txt name)", async () => {
    renderImage("image/png", "data.txt");
    expect(await screen.findByAltText("data.txt")).toBeDefined();
  });
});

describe("CodeViewer markdown preview (Streamdown)", () => {
  it("renders Markdown via Streamdown in preview mode — headings + GFM task lists, not raw source or Monaco", () => {
    const md = "# Title\n\n- [x] done\n- [ ] todo\n";
    const { container } = renderViewer(md, true, "notes.md", { viewMode: "preview" });

    // The heading is rendered as a Streamdown element, so the literal "# Title"
    // source never appears — i.e. this is the rendered preview, not source.
    const heading = container.querySelector('[data-streamdown="heading-1"]');
    expect(heading?.textContent).toContain("Title");
    expect(screen.queryByText("# Title")).toBeNull();

    // GFM task list: remark-gfm tags each item `task-list-item` with a checkbox
    // whose checked state reflects [x] vs [ ] (this is what PR #721's CSS targets).
    const checkboxes = container.querySelectorAll<HTMLInputElement>(
      '[data-streamdown="list-item"].task-list-item input[type="checkbox"]',
    );
    expect(checkboxes.length).toBe(2);
    expect(checkboxes[0].checked).toBe(true);
    expect(checkboxes[1].checked).toBe(false);

    // Preview must not fall through to the Monaco source editor.
    expect(screen.queryByTestId("monaco-editor-stub")).toBeNull();
  });
});
