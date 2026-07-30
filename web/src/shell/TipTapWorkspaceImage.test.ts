/**
 * Unit tests for the workspace-aware TipTap Image extension:
 *   - resolveWorkspacePath / isWorkspaceRelativeSrc helpers
 *   - markdown parse + byte-faithful serialisation of image nodes
 *   - the node view's authenticated fetch → blob URL pipeline for
 *     workspace-relative srcs (external URLs render directly)
 *
 * Each editor test creates a real TipTap Editor (real schema, real
 * @tiptap/markdown parsing) so a regression in parse/serialise/node-view
 * behaviour fails the test. Only the HTTP boundary (fetchFileContent) is
 * mocked, returning a real FileContentResponse-shaped object.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Editor } from "@tiptap/core";
import { Markdown } from "@tiptap/markdown";
import StarterKit from "@tiptap/starter-kit";
import {
  createWorkspaceImageExtension,
  ImageAwareLink,
  isWorkspaceRelativeSrc,
  resolveWorkspacePath,
} from "./TipTapWorkspaceImage";
import type { FileContentResponse } from "@/hooks/useFileContent";

vi.mock("@/hooks/useFileContent", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useFileContent")>();
  // Keep the real fileContentToBlob (the decode logic under test); only the
  // network call is faked.
  return { ...actual, fetchFileContent: vi.fn() };
});
import { fetchFileContent } from "@/hooks/useFileContent";

// jsdom has no createObjectURL/revokeObjectURL; the node view needs both.
// Unique URL per call (like the real API) so identity assertions can't pass
// by accident when multiple blobs are created.
let blobUrlCounter = 0;
const createObjectURL = vi.fn((_blob: Blob | MediaSource) => `blob:mock-${++blobUrlCounter}`);
const revokeObjectURL = vi.fn();
// jsdom leaves these undefined; remember whatever was there so the stubs
// don't leak into other test files in the same worker.
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  // Default so node views created during editor mount always get a promise;
  // individual tests override with their own resolution/rejection.
  vi.mocked(fetchFileContent).mockResolvedValue(PNG_RESPONSE);
});

let editor: Editor | null = null;
afterEach(() => {
  editor?.destroy();
  editor = null;
  vi.clearAllMocks();
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

/** PNG-ish bytes, base64-encoded, as the filesystem API returns binaries. */
const PNG_RESPONSE: FileContentResponse = {
  object: "session.environment.filesystem.file_content",
  path: "docs/images/logo.png",
  content_type: "image/png",
  encoding: "base64",
  content: btoa("\x89PNG-bytes"),
  bytes: 9,
};

/** Mounted editor so node views render into a real DOM element. */
function makeEditor(markdown: string, filePath = "README.md"): Editor {
  return new Editor({
    element: document.createElement("div"),
    extensions: [
      // Same stack as MarkdownRichTextViewer: StarterKit's bundled Link is
      // disabled so ImageAwareLink owns the markdown link token.
      StarterKit.configure({ link: false }),
      ImageAwareLink.configure({ openOnClick: false, autolink: false }),
      Markdown,
      createWorkspaceImageExtension("conv_test1", filePath),
    ],
    content: markdown,
    contentType: "markdown",
  });
}

// ---------------------------------------------------------------------------
// Path helpers
// ---------------------------------------------------------------------------

describe("isWorkspaceRelativeSrc", () => {
  it("treats workspace paths as relative and URLs/data URIs as external", () => {
    expect(isWorkspaceRelativeSrc("docs/x.png")).toBe(true);
    expect(isWorkspaceRelativeSrc("./x.png")).toBe(true);
    expect(isWorkspaceRelativeSrc("../x.png")).toBe(true);
    expect(isWorkspaceRelativeSrc("/docs/x.png")).toBe(true);
    expect(isWorkspaceRelativeSrc("https://img.shields.io/badge.svg")).toBe(false);
    expect(isWorkspaceRelativeSrc("http://example.com/x.png")).toBe(false);
    expect(isWorkspaceRelativeSrc("data:image/png;base64,AAAA")).toBe(false);
    expect(isWorkspaceRelativeSrc("//cdn.example.com/x.png")).toBe(false);
  });
});

describe("resolveWorkspacePath", () => {
  it("resolves against the open file's directory", () => {
    expect(resolveWorkspacePath("README.md", "docs/images/x.png")).toBe("docs/images/x.png");
    expect(resolveWorkspacePath("docs/guide.md", "images/x.png")).toBe("docs/images/x.png");
    expect(resolveWorkspacePath("docs/guide.md", "./images/x.png")).toBe("docs/images/x.png");
  });

  it("normalises .. segments and clamps at the workspace root", () => {
    expect(resolveWorkspacePath("docs/sub/guide.md", "../images/x.png")).toBe("docs/images/x.png");
    expect(resolveWorkspacePath("README.md", "../../x.png")).toBe("x.png");
  });

  it("treats a leading slash as workspace-root-relative (GitHub-style)", () => {
    expect(resolveWorkspacePath("docs/guide.md", "/assets/x.png")).toBe("assets/x.png");
  });

  it("strips query/fragment suffixes and decodes percent-escapes", () => {
    expect(resolveWorkspacePath("README.md", "img/x.png?raw=true#frag")).toBe("img/x.png");
    expect(resolveWorkspacePath("README.md", "img/my%20logo.png")).toBe("img/my logo.png");
  });

  it("keeps segments verbatim when decoding would smuggle in separators", () => {
    // A decoded %2F would re-split into "../.." downstream — the segment
    // must stay one opaque (still-encoded) unit instead.
    expect(resolveWorkspacePath("docs/guide.md", "..%2F..%2Fsecret.png")).toBe(
      "docs/..%2F..%2Fsecret.png",
    );
    expect(resolveWorkspacePath("README.md", "a%5Cb/x.png")).toBe("a%5Cb/x.png");
  });
});

// ---------------------------------------------------------------------------
// Markdown parse + serialise
// ---------------------------------------------------------------------------

describe("markdown round-trip", () => {
  it("parses ![alt](src) into an image node and serialises it back unchanged", () => {
    const md = "before\n\n![the logo](docs/images/logo.png)\n\nafter";
    editor = makeEditor(md);
    let image: { src?: string; alt?: string } | null = null;
    editor.state.doc.descendants((node) => {
      if (node.type.name === "image") image = node.attrs as { src?: string; alt?: string };
    });
    expect(image).not.toBeNull();
    // The node keeps the ORIGINAL relative path — never a blob/API URL.
    expect(image!.src).toBe("docs/images/logo.png");
    expect(image!.alt).toBe("the logo");
    expect(editor.getMarkdown()).toContain("![the logo](docs/images/logo.png)");
  });

  it("keeps badge images nested inside links", () => {
    const md = "[![License](https://img.shields.io/badge/l.svg)](LICENSE)";
    editor = makeEditor(md);
    // Exact match: also catches double-wrapping if a future @tiptap/markdown
    // starts serialising marks around inline atoms itself.
    expect(editor.getMarkdown().trim()).toBe(md);
    // The DOM nests the image inside the link so the badge is clickable.
    expect(editor.view.dom.querySelector("a[href='LICENSE'] img")).not.toBeNull();
  });

  it("escapes ] in alt text so the serialised image re-parses intact", () => {
    const md = "![a\\]b](x.png)";
    editor = makeEditor(md);
    // Byte-faithful: the source had to escape the ] itself, so serialising
    // the parsed alt ("a]b") must re-emit the same escape. An unescaped ]
    // would close the label early and corrupt the file on save.
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it("wraps destinations containing spaces in angle brackets", () => {
    const md = "![logo](<my file.png>)";
    editor = makeEditor(md, "docs/guide.md");
    // A bare destination with a space is invalid CommonMark — serialising
    // without <> would truncate the path at the space on re-parse.
    expect(editor.getMarkdown().trim()).toBe(md);
    // The node view still resolves/fetches the decoded path.
    expect(fetchFileContent).toHaveBeenCalledWith("conv_test1", "docs/my file.png");
  });

  it("keeps balanced-paren destinations bare (byte-faithful)", () => {
    const md = "![a](x(1).png)";
    editor = makeEditor(md);
    // Balanced parens are legal bare per CommonMark — wrapping in <> would
    // churn bytes for files that wrote the destination unwrapped.
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it('escapes " in titles', () => {
    const md = '![a](x.png "say \\"hi\\"")';
    editor = makeEditor(md);
    // An unescaped " would close the title early on re-parse.
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it("keeps the link title on linked badges", () => {
    const md = '[![License](https://img.shields.io/badge/l.svg)](LICENSE "The license")';
    editor = makeEditor(md);
    // ImageAwareLink stores the title in the link mark attrs; the image
    // serialiser must re-emit it or the file is rewritten on save.
    expect(editor.getMarkdown().trim()).toBe(md);
  });

  it("serialises sized images (from HTML <img>) back to HTML preserving dimensions", () => {
    const md = '<img src="docs/images/hero.png" alt="hero" width="520" />';
    editor = makeEditor(md);
    const out = editor.getMarkdown();
    expect(out).toContain('src="docs/images/hero.png"');
    expect(out).toContain('width="520"');
  });
});

// ---------------------------------------------------------------------------
// Node view rendering
// ---------------------------------------------------------------------------

describe("node view", () => {
  it("fetches workspace-relative images through the filesystem API into a blob URL", async () => {
    vi.mocked(fetchFileContent).mockResolvedValue(PNG_RESPONSE);
    editor = makeEditor("![logo](images/logo.png)", "docs/guide.md");
    // Resolution happened relative to the open file's directory.
    expect(fetchFileContent).toHaveBeenCalledWith("conv_test1", "docs/images/logo.png");
    const img = editor.view.dom.querySelector("img");
    expect(img).not.toBeNull();
    // The src must be the exact URL createObjectURL handed back for the blob.
    await vi.waitFor(() =>
      expect(img!.getAttribute("src")).toBe(createObjectURL.mock.results[0].value),
    );
    // The blob was built with the API's content type.
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("image/png");
    // Serialisation still emits the original path, not the blob URL.
    expect(editor.getMarkdown()).toContain("![logo](images/logo.png)");
  });

  it("does not fetch when the src is empty", () => {
    editor = makeEditor("![empty]()");
    const img = editor.view.dom.querySelector("img");
    // No src attribute at all — alt text is the visible fallback.
    expect(img!.getAttribute("src")).toBeNull();
    expect(fetchFileContent).not.toHaveBeenCalled();
  });

  it.each(["/", "#frag", "?raw=true"])(
    "does not fetch when the src has no usable path component (%s)",
    (src) => {
      editor = makeEditor(`![x](${src})`, "docs/guide.md");
      // These resolve to the workspace root ("") or the file's directory —
      // never a fetchable file. A call here is a guaranteed-failing request
      // (or a directory listing rendered as a broken image blob).
      expect(fetchFileContent).not.toHaveBeenCalled();
      // Alt-text fallback, same as an empty src.
      expect(editor.view.dom.querySelector("img")!.getAttribute("src")).toBeNull();
    },
  );

  it("renders external URLs directly without hitting the filesystem API", () => {
    editor = makeEditor("![badge](https://img.shields.io/badge/l.svg)");
    const img = editor.view.dom.querySelector("img");
    expect(img!.getAttribute("src")).toBe("https://img.shields.io/badge/l.svg");
    expect(fetchFileContent).not.toHaveBeenCalled();
  });

  it("leaves src unset when the workspace fetch fails (alt-text fallback)", async () => {
    vi.mocked(fetchFileContent).mockRejectedValue(new Error("404 Not Found"));
    editor = makeEditor("![missing](nope.png)");
    const img = editor.view.dom.querySelector("img");
    expect(img!.getAttribute("alt")).toBe("missing");
    // Let the rejected fetch settle, then confirm no src was applied.
    await vi.waitFor(() => expect(fetchFileContent).toHaveBeenCalled());
    await Promise.resolve();
    expect(img!.getAttribute("src")).toBeNull();
    expect(createObjectURL).not.toHaveBeenCalled();
  });

  it("does not refetch the image when non-src attrs change (e.g. alt)", async () => {
    vi.mocked(fetchFileContent).mockResolvedValue(PNG_RESPONSE);
    editor = makeEditor("start ![logo](logo.png) end");
    const img = editor.view.dom.querySelector("img");
    await vi.waitFor(() => expect(img!.getAttribute("src")).toMatch(/^blob:mock-/));
    const renderedUrl = img!.getAttribute("src");
    // Change a presentational attr. An attr change makes the node compare
    // unequal, so WITHOUT update() ProseMirror tears the view down and the
    // image refetches; update() must absorb it instead. (Plain text edits
    // next to the image never recreate the view — ProseMirror reuses
    // unchanged node descs — so an attr change is the discriminating case.)
    let imagePos = -1;
    editor.state.doc.descendants((node, pos) => {
      if (node.type.name === "image") imagePos = pos;
    });
    editor.commands.command(({ tr, state }) => {
      const node = state.doc.nodeAt(imagePos)!;
      tr.setNodeMarkup(imagePos, undefined, { ...node.attrs, alt: "new alt" });
      return true;
    });
    // Exactly one fetch/blob: a second call means update() declined the
    // attr change and the view was recreated, refetching needlessly.
    expect(fetchFileContent).toHaveBeenCalledTimes(1);
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const updatedImg = editor.view.dom.querySelector("img");
    // The original element (and its blob URL) survived the attr change…
    expect(updatedImg!.getAttribute("src")).toBe(renderedUrl);
    // …and update() synced the new attr value onto it.
    expect(updatedImg!.getAttribute("alt")).toBe("new alt");
  });

  it("refetches and revokes the old blob URL when src changes", async () => {
    vi.mocked(fetchFileContent).mockResolvedValue(PNG_RESPONSE);
    editor = makeEditor("![logo](logo.png)");
    const img = editor.view.dom.querySelector("img");
    await vi.waitFor(() => expect(img!.getAttribute("src")).toMatch(/^blob:mock-/));
    const firstUrl = img!.getAttribute("src");
    // Change the image node's src via the document (public commands API).
    let imagePos = -1;
    editor.state.doc.descendants((node, pos) => {
      if (node.type.name === "image") imagePos = pos;
    });
    editor.commands.command(({ tr, state }) => {
      const node = state.doc.nodeAt(imagePos)!;
      tr.setNodeMarkup(imagePos, undefined, { ...node.attrs, src: "other.png" });
      return true;
    });
    const newImg = editor.view.dom.querySelector("img");
    // src change must decline update(): the view is recreated and the new
    // path fetched. One call total would mean the stale image kept showing.
    await vi.waitFor(() => expect(fetchFileContent).toHaveBeenCalledTimes(2));
    expect(fetchFileContent).toHaveBeenLastCalledWith("conv_test1", "other.png");
    await vi.waitFor(() => expect(newImg!.getAttribute("src")).not.toBe(firstUrl));
    // The replaced view's blob URL was revoked — otherwise each src change
    // leaks one object URL for the session's lifetime.
    expect(revokeObjectURL).toHaveBeenCalledWith(firstUrl);
  });

  it("revokes the blob URL when the editor is destroyed", async () => {
    vi.mocked(fetchFileContent).mockResolvedValue(PNG_RESPONSE);
    editor = makeEditor("![logo](logo.png)");
    const img = editor.view.dom.querySelector("img");
    await vi.waitFor(() => expect(img!.getAttribute("src")).toMatch(/^blob:mock-/));
    // Destroy must revoke the SAME URL that was rendered, not just any URL.
    const renderedUrl = img!.getAttribute("src");
    editor.destroy();
    editor = null;
    expect(revokeObjectURL).toHaveBeenCalledWith(renderedUrl);
  });

  it("renders explicit width/height as inline style, not attributes", () => {
    // GitHub honors <img width/height>, but Tailwind Preflight's
    // `img { height: auto }` overrides the width/height *attributes*, so a
    // sized image would render square. The node view must put the dimensions
    // on the inline style (which wins the cascade) — matching the preview.
    editor = makeEditor(
      '<img src="https://example.com/logo.png" alt="logo" width="200" height="100" />',
    );
    const img = editor.view.dom.querySelector("img")!;
    expect(img.style.width).toBe("200px");
    expect(img.style.height).toBe("100px");
    // The presentational attributes are removed so Preflight can't win.
    expect(img.hasAttribute("width")).toBe(false);
    expect(img.hasAttribute("height")).toBe(false);
    // Serialisation is unaffected — dimensions still round-trip to HTML.
    const out = editor.getMarkdown();
    expect(out).toContain('width="200"');
    expect(out).toContain('height="100"');
  });

  it("clears the inline dimension style when a dimension attr is removed", () => {
    editor = makeEditor(
      '<img src="https://example.com/logo.png" alt="logo" width="200" height="100" />',
    );
    const img = editor.view.dom.querySelector("img")!;
    expect(img.style.height).toBe("100px");
    // Drop just the height attr; update() must clear the inline style too, or
    // the removed dimension would visually persist.
    let imagePos = -1;
    editor.state.doc.descendants((node, pos) => {
      if (node.type.name === "image") imagePos = pos;
    });
    editor.commands.command(({ tr, state }) => {
      const node = state.doc.nodeAt(imagePos)!;
      tr.setNodeMarkup(imagePos, undefined, { ...node.attrs, height: null });
      return true;
    });
    expect(editor.view.dom.querySelector("img")!.style.height).toBe("");
    // The other dimension is untouched.
    expect(editor.view.dom.querySelector("img")!.style.width).toBe("200px");
  });
});
