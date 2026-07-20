import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { useFileContent } from "@/hooks/useFileContent";
import { CodeViewer } from "./CodeViewer";
import { ImageLightboxProvider } from "@/components/ImageLightbox";
import { HTML_PREVIEW_SANDBOX } from "./codeViewerHelpers";

// ── Module mocks ──────────────────────────────────────────────────────────────

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: vi.fn() }));
// Stub Shiki so the highlighting effect never fires an async callback that
// would mutate state after the test cleans up.
vi.mock("@/components/ai-elements/code-block", () => ({
  highlightCode: vi.fn(() => null),
  // NotebookPreview renders notebook code cells through CodeBlockContent.
  CodeBlockContent: ({ code }: { code: string }) => <pre>{code}</pre>,
}));
vi.mock("./MarkdownRichTextViewer", () => ({ MarkdownRichTextViewer: () => null }));
// Stub the lazy Monaco editor so the heavy monaco-editor bundle isn't loaded in
// jsdom; its presence in the DOM is the signal that a file was routed to Monaco.
vi.mock("./MonacoCodeEditor", () => ({
  MonacoCodeEditor: () => <div data-testid="monaco-editor-stub" />,
}));
// Stub the lazy PdfViewer so react-pdf / the pdf.js worker (no PDF engine in
// jsdom) never load; its testid presence is the signal that a file was routed
// to the PDF surface.
vi.mock("./PdfViewer", () => ({
  PdfViewer: () => <div data-testid="pdf-viewer-stub" />,
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

// A tiny base64 blob standing in for a PDF's bytes — the stubbed PdfViewer never
// parses it, so any base64 payload with the application/pdf content type is enough
// to exercise routing.
const PDF_BASE64 = "JVBERi0xLjQK";

function makePdfQuery(
  contentType: string | null = "application/pdf",
  truncated = false,
): ReturnType<typeof useFileContent> {
  return {
    data: { content: PDF_BASE64, encoding: "base64", content_type: contentType, truncated },
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

describe("CodeViewer markdown preview rendering (issue #970)", () => {
  // The read-only markdown preview is now the default surface for .md files, so
  // it must faithfully render the GFM feature set the issue calls out:
  // headings, lists, tables, code blocks, blockquotes, task lists, emoji.
  const renderMd = (content: string) =>
    renderViewer(content, true, "doc.md", { viewMode: "preview" });

  it("renders headings", () => {
    const { container } = renderMd("# Title\n\n## Subtitle");
    expect(container.querySelector("h1")?.textContent).toBe("Title");
    expect(container.querySelector("h2")?.textContent).toBe("Subtitle");
  });

  it("renders bullet and ordered lists", () => {
    const { container } = renderMd("- one\n- two\n\n1. first\n2. second");
    expect(container.querySelectorAll("ul li")).toHaveLength(2);
    expect(container.querySelectorAll("ol li")).toHaveLength(2);
  });

  it("renders GFM tables", () => {
    const { container } = renderMd("| A | B |\n| - | - |\n| 1 | 2 |");
    expect(container.querySelector("table")).not.toBeNull();
    expect(container.querySelectorAll("th")).toHaveLength(2);
    expect(container.querySelectorAll("tbody td")).toHaveLength(2);
  });

  it("renders fenced code blocks", () => {
    const { container } = renderMd("```js\nconst x = 1;\n```");
    expect(container.querySelector("pre code")?.textContent).toContain("const x = 1;");
  });

  it("renders blockquotes", () => {
    const { container } = renderMd("> quoted text");
    expect(container.querySelector("blockquote")?.textContent).toContain("quoted text");
  });

  it("renders GFM task lists as checkboxes reflecting their checked state", () => {
    const { container } = renderMd("- [x] done\n- [ ] todo");
    const boxes = container.querySelectorAll<HTMLInputElement>('input[type="checkbox"]');
    expect(boxes).toHaveLength(2);
    expect(boxes[0].checked).toBe(true);
    expect(boxes[1].checked).toBe(false);
  });

  it("renders :shortcode: emoji as their unicode glyphs", () => {
    // GitHub renders `:tada:` as 🎉; the preview matches that so agent-authored
    // docs/summaries read the same here as on GitHub.
    const { container } = renderMd("Ship it :tada: :rocket:");
    expect(container.textContent).toContain("🎉");
    expect(container.textContent).toContain("🚀");
  });

  it("renders embedded raw HTML that GitHub supports", () => {
    // Markdown routinely embeds raw HTML (collapsible sections, sub/superscript,
    // keyboard keys, line breaks). Without rehype-raw these render as escaped
    // literal tags; the preview must render them as real elements like GitHub.
    const { container } = renderMd(
      [
        "<details><summary>More</summary>Hidden body</details>",
        "",
        "H<sub>2</sub>O and E=mc<sup>2</sup>",
        "",
        "press <kbd>Enter</kbd>",
        "",
        "line one<br>line two",
      ].join("\n"),
    );
    expect(container.querySelector("details summary")?.textContent).toBe("More");
    expect(container.querySelector("sub")?.textContent).toBe("2");
    expect(container.querySelector("sup")?.textContent).toBe("2");
    expect(container.querySelector("kbd")?.textContent).toBe("Enter");
    expect(container.querySelector("br")).not.toBeNull();
    // The literal, un-rendered tag must not leak through as visible text.
    expect(container.textContent).not.toContain("<details>");
  });

  it("sanitizes dangerous raw HTML (no scripts, event handlers, or js: URLs)", () => {
    // Markdown content is untrusted (agent/user-authored). rehype-raw parses raw
    // HTML, so rehype-sanitize must strip anything executable before it renders
    // inline in the host document (unlike the iframe-isolated HTML preview).
    const { container } = renderMd(
      "<script>window.__pwned = true</script>\n\n" +
        '<img src="x" onerror="window.__pwned = true" alt="x">\n\n' +
        '<a href="javascript:window.__pwned = true">click</a>',
    );
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")?.hasAttribute("onerror")).toBe(false);
    // A javascript: href is dropped entirely rather than left clickable.
    expect(container.querySelector("a")?.getAttribute("href")).toBeNull();
  });

  it("renders GitHub alerts as typed callouts, not literal blockquote text", () => {
    // `> [!NOTE]` etc. are GitHub alerts. GFM alone leaves them as plain
    // blockquotes with a literal "[!NOTE]" first line; rehype-github-alerts
    // turns them into typed callouts (a .markdown-alert wrapper + a titled
    // header) that CSS then styles per type, matching GitHub and the editor.
    const { container } = renderMd(
      ["> [!NOTE]\n> Useful information.", "", "> [!WARNING]\n> Careful here."].join("\n"),
    );
    const note = container.querySelector(".markdown-alert.markdown-alert-note");
    const warning = container.querySelector(".markdown-alert.markdown-alert-warning");
    expect(note).not.toBeNull();
    expect(warning).not.toBeNull();
    expect(note?.querySelector(".markdown-alert-title")?.textContent).toBe("Note");
    expect(note?.textContent).toContain("Useful information.");
    // The raw marker must be consumed, not shown verbatim.
    expect(container.textContent).not.toContain("[!NOTE]");
  });

  it("keeps explicit width/height on an embedded <img>", () => {
    // GitHub honors <img width/height>. Tailwind Preflight's `img { height:
    // auto }` overrides the HTML attributes, so the preview forwards them to an
    // inline style (which wins the cascade) — the sized image is not left square.
    const { container } = renderMd(
      '<img src="https://example.com/logo.png" alt="logo" width="200" height="100">',
    );
    const img = container.querySelector<HTMLImageElement>('img[alt="logo"]');
    expect(img).not.toBeNull();
    expect(img?.style.width).toBe("200px");
    expect(img?.style.height).toBe("100px");
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

  it("opens the shared zoom lightbox when the image is clicked", async () => {
    render(
      <ImageLightboxProvider>
        <CodeViewer
          conversationId="conv_1"
          path="assets/logo.png"
          fileQuery={makeImageQuery("image/png")}
          comments={[]}
          activeSelection={null}
          onSetActiveSelection={() => {}}
          panelOpen={true}
          searchOpen={false}
          setSearchOpen={() => {}}
          searchInputRef={noopRef}
          viewMode="source"
        />
      </ImageLightboxProvider>,
    );

    const img = (await screen.findByAltText("logo.png")) as HTMLImageElement;
    // No lightbox until the user clicks the inline image.
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.click(img);

    // The Radix Dialog is now open with the zoom controls from the lightbox.
    expect(await screen.findByRole("dialog")).toBeDefined();
    expect(screen.getByLabelText("Zoom in")).toBeDefined();
    expect(screen.getByLabelText("Zoom out")).toBeDefined();
  });
});

describe("CodeViewer PDF routing", () => {
  function renderPdf(
    contentType: string | null = "application/pdf",
    path = "report.pdf",
    truncated = false,
  ) {
    return render(
      <CodeViewer
        conversationId="conv_1"
        path={path}
        fileQuery={makePdfQuery(contentType, truncated)}
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

  it("routes a .pdf to the PDF viewer, not the binary placeholder", async () => {
    renderPdf();
    expect(await screen.findByTestId("pdf-viewer-stub")).toBeDefined();
    expect(screen.queryByText(/binary file/i)).toBeNull();
    expect(screen.queryByTestId("monaco-editor-stub")).toBeNull();
  });

  it("routes by content_type over extension (pdf MIME on a .bin name)", async () => {
    renderPdf("application/pdf", "data.bin");
    expect(await screen.findByTestId("pdf-viewer-stub")).toBeDefined();
  });

  it("falls back to the .pdf extension when content type is null", async () => {
    renderPdf(null, "report.pdf");
    expect(await screen.findByTestId("pdf-viewer-stub")).toBeDefined();
  });
});

describe("CodeViewer .ipynb routing", () => {
  const MINIMAL_NB = JSON.stringify({
    nbformat: 4,
    cells: [{ cell_type: "markdown", metadata: {}, source: ["# Notebook Title\n"] }],
  });

  it("renders the notebook preview in preview mode", () => {
    renderViewer(MINIMAL_NB, true, "analysis.ipynb", { viewMode: "preview" });
    expect(screen.getByRole("heading", { name: "Notebook Title" })).toBeDefined();
    expect(screen.queryByTestId("monaco-editor-stub")).toBeNull();
  });

  it("keeps raw-JSON Monaco as the source-view escape hatch", () => {
    renderViewer(MINIMAL_NB, true, "analysis.ipynb", { viewMode: "source" });
    expect(screen.getByTestId("monaco-editor-stub")).toBeDefined();
  });

  it("warns about truncated notebooks instead of rendering silently-incomplete cells", () => {
    renderViewer(MINIMAL_NB.slice(0, 40), true, "analysis.ipynb", {
      viewMode: "preview",
      truncated: true,
    });
    // Truncated JSON cannot parse — the preview shows its parse-error state…
    expect(screen.getByText(/Cannot render notebook/)).toBeDefined();
    // …and the shared truncation banner is stacked above it.
    expect(screen.getByText(/truncated/i)).toBeDefined();
  });
});
