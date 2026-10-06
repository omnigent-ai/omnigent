import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  HTML_PREVIEW_HEAD,
  HTML_PREVIEW_SANDBOX,
  detectLang,
  getSelectionOffsets,
  indexToLine,
  getModelFormat,
  isBinaryPath,
  isImageFile,
  isModelFile,
  isNotebookPath,
  isPdfFile,
  lineOverlapsSelection,
  modelViewerTheme,
  openHtmlArtifactInNewTab,
  prepareHtmlPreviewDoc,
} from "./codeViewerHelpers";

// ---------------------------------------------------------------------------
// detectLang — language matrix backing syntax highlighting
// ---------------------------------------------------------------------------

describe("detectLang", () => {
  // Each extension must resolve to its Shiki BundledLanguage, not the "text" default.
  it.each([
    ["app.py", "python"],
    ["mod.rs", "rust"],
    ["main.go", "go"],
    ["index.ts", "typescript"],
    ["component.tsx", "tsx"],
    ["script.js", "javascript"],
    ["widget.jsx", "jsx"],
    ["config.json", "json"],
    ["values.yaml", "yaml"],
    ["values.yml", "yaml"],
    ["pyproject.toml", "toml"],
    ["README.md", "markdown"],
    ["run.sh", "bash"],
    ["profile.bash", "bash"],
    ["aliases.zsh", "bash"],
    ["query.sql", "sql"],
    ["page.html", "html"],
    ["styles.css", "css"],
  ])("maps %s to %s", (path, expected) => {
    expect(detectLang(path)).toBe(expected);
  });

  it("is case-insensitive on the extension", () => {
    expect(detectLang("Main.PY")).toBe("python");
    expect(detectLang("NOTES.MD")).toBe("markdown");
  });

  it("falls back to 'text' for unknown or extension-less paths", () => {
    expect(detectLang("data.unknownext")).toBe("text");
    expect(detectLang("LICENSE")).toBe("text");
  });

  it("highlights Scala source files", () => {
    expect(detectLang("Service.scala")).toBe("scala");
    expect(detectLang("build.sc")).toBe("scala");
  });

  it("highlights files identified by name rather than extension", () => {
    expect(detectLang("Dockerfile")).toBe("dockerfile");
    expect(detectLang("path/to/Makefile")).toBe("make");
    expect(detectLang("CMakeLists.txt")).toBe("cmake");
  });

  it("highlights a sampling of the extended language map", () => {
    expect(detectLang("Main.kt")).toBe("kotlin");
    expect(detectLang("app.rb")).toBe("ruby");
    expect(detectLang("index.php")).toBe("php");
    expect(detectLang("View.swift")).toBe("swift");
    expect(detectLang("styles.scss")).toBe("scss");
    expect(detectLang("App.vue")).toBe("vue");
    expect(detectLang("schema.graphql")).toBe("graphql");
    expect(detectLang("Program.cs")).toBe("csharp");
  });
});

// ---------------------------------------------------------------------------
// isBinaryPath — binary-file fallback
// ---------------------------------------------------------------------------

describe("isBinaryPath", () => {
  it.each([
    "logo.png",
    "photo.jpg",
    "scan.jpeg",
    "icon.ico",
    "archive.zip",
    "bundle.tar",
    "data.gz",
    "app.exe",
    "lib.so",
    "font.woff2",
    "clip.mp4",
    "module.pyc",
    "store.sqlite3",
  ])("classifies %s as binary", (path) => {
    expect(isBinaryPath(path)).toBe(true);
  });

  it.each(["app.py", "index.ts", "README.md", "config.json", "notes.txt"])(
    "classifies %s as non-binary",
    (path) => {
      expect(isBinaryPath(path)).toBe(false);
    },
  );

  it("is case-insensitive on the extension", () => {
    expect(isBinaryPath("LOGO.PNG")).toBe(true);
  });

  it("treats extension-less paths as non-binary", () => {
    expect(isBinaryPath("Dockerfile")).toBe(false);
  });
});

describe("isNotebookPath", () => {
  it.each(["analysis.ipynb", "dir/Report.IPYNB", "a.b.ipynb"])(
    "classifies %s as a notebook",
    (path) => {
      expect(isNotebookPath(path)).toBe(true);
    },
  );

  it.each(["notes.md", "data.json", "ipynb", "nb.ipynb.bak"])(
    "classifies %s as not a notebook",
    (path) => {
      expect(isNotebookPath(path)).toBe(false);
    },
  );
});

// ---------------------------------------------------------------------------
// isImageFile — image-preview detection (MIME-first, extension fallback)
// ---------------------------------------------------------------------------

describe("isImageFile", () => {
  it.each([
    "logo.png",
    "photo.jpg",
    "scan.jpeg",
    "anim.gif",
    "icon.ico",
    "hero.webp",
    "next.avif",
    "diagram.svg",
  ])("classifies %s as an image by extension", (path) => {
    expect(isImageFile(path)).toBe(true);
  });

  it.each(["app.py", "archive.zip", "clip.mp4", "font.woff2", "notes.txt"])(
    "classifies %s as non-image by extension",
    (path) => {
      expect(isImageFile(path)).toBe(false);
    },
  );

  it("is case-insensitive on the extension", () => {
    expect(isImageFile("LOGO.PNG")).toBe(true);
  });

  it("treats a content type as authoritative over the extension", () => {
    // A misleading/extension-less name still previews when the server says image.
    expect(isImageFile("blob", "image/png")).toBe(true);
    expect(isImageFile("data.txt", "image/jpeg")).toBe(true);
    // ...and an image extension is overridden by a non-image content type.
    expect(isImageFile("logo.png", "text/plain")).toBe(false);
    expect(isImageFile("photo.jpg", "application/octet-stream")).toBe(false);
  });

  it("falls back to the extension when content type is null/undefined", () => {
    expect(isImageFile("logo.png", null)).toBe(true);
    expect(isImageFile("logo.png", undefined)).toBe(true);
    expect(isImageFile("notes.txt", null)).toBe(false);
  });

  it("treats extension-less paths with no content type as non-image", () => {
    expect(isImageFile("Dockerfile")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// isPdfFile — PDF-preview detection (MIME-first, extension fallback)
// ---------------------------------------------------------------------------

describe("isPdfFile", () => {
  it("classifies a .pdf by extension", () => {
    expect(isPdfFile("report.pdf")).toBe(true);
  });

  it.each(["app.py", "logo.png", "notes.txt", "archive.zip"])(
    "classifies %s as non-pdf by extension",
    (path) => {
      expect(isPdfFile(path)).toBe(false);
    },
  );

  it("is case-insensitive on the extension", () => {
    expect(isPdfFile("REPORT.PDF")).toBe(true);
  });

  it("treats a content type as authoritative over the extension", () => {
    // A misleading/extension-less name still previews when the server says PDF.
    expect(isPdfFile("blob", "application/pdf")).toBe(true);
    expect(isPdfFile("data.bin", "application/pdf")).toBe(true);
    // ...and a .pdf extension is overridden by a non-pdf content type.
    expect(isPdfFile("report.pdf", "text/plain")).toBe(false);
    expect(isPdfFile("report.pdf", "application/octet-stream")).toBe(false);
  });

  it("falls back to the extension when content type is null/undefined", () => {
    expect(isPdfFile("report.pdf", null)).toBe(true);
    expect(isPdfFile("report.pdf", undefined)).toBe(true);
    expect(isPdfFile("notes.txt", null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// isModelFile — 3D model preview detection (STL / 3MF / OBJ)
// ---------------------------------------------------------------------------

describe("isModelFile", () => {
  it.each(["part.stl", "assembly.3mf", "mesh.obj", "dir/nested/widget.STL", "MODEL.OBJ"])(
    "classifies %s as a model by extension",
    (path) => {
      expect(isModelFile(path)).toBe(true);
    },
  );

  it.each([
    "app.py",
    "logo.png",
    "scene.gltf",
    "model.glb",
    "part.step",
    "part.stp",
    "notes.txt",
    "data.json",
  ])("classifies %s as non-model (out of scope or unrelated)", (path) => {
    expect(isModelFile(path)).toBe(false);
  });

  it("treats a recognized model content type as authoritative on an unknown extension", () => {
    expect(isModelFile("blob", "model/stl")).toBe(true);
    expect(isModelFile("download", "application/vnd.ms-pki.stl")).toBe(true);
    expect(isModelFile("blob", "model/3mf")).toBe(true);
    expect(isModelFile("blob", "model/obj")).toBe(true);
  });

  it("ignores charset parameters on the content type", () => {
    expect(isModelFile("blob", "model/obj; charset=utf-8")).toBe(true);
  });

  it("still previews a model extension even when the server reports a generic type", () => {
    // Binary STL/3MF are commonly served as octet-stream and ASCII OBJ as
    // text/plain — the extension must win so they don't fall to the binary or
    // raw-text paths.
    expect(isModelFile("part.stl", "application/octet-stream")).toBe(true);
    expect(isModelFile("mesh.obj", "text/plain")).toBe(true);
  });

  it("does not treat plain text (no model extension) as a model", () => {
    expect(isModelFile("readme.txt", "text/plain")).toBe(false);
  });

  it("treats extension-less paths with no content type as non-model", () => {
    expect(isModelFile("Dockerfile")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// getModelFormat — the SINGLE resolver shared by dispatch and loader selection
// ---------------------------------------------------------------------------

describe("getModelFormat", () => {
  it.each([
    ["part.stl", "stl"],
    ["assembly.3mf", "3mf"],
    ["mesh.obj", "obj"],
    ["dir/WIDGET.STL", "stl"],
  ])("resolves %s to %s by extension", (path, format) => {
    expect(getModelFormat(path)).toBe(format);
  });

  it("resolves a recognized model MIME to the matching loader on an unknown extension", () => {
    // MIME-first: a file with no/unknown extension still selects the correct
    // loader — this is what keeps dispatch and parsing in lockstep.
    expect(getModelFormat("blob", "model/stl")).toBe("stl");
    expect(getModelFormat("download.bin", "application/vnd.ms-pki.stl")).toBe("stl");
    expect(getModelFormat("blob", "model/3mf")).toBe("3mf");
    expect(getModelFormat("blob", "model/obj")).toBe("obj");
  });

  it("prefers the MIME format over the extension when both are model types", () => {
    // A .obj served with an STL MIME resolves to the STL loader (MIME-first).
    expect(getModelFormat("weird.obj", "model/stl")).toBe("stl");
  });

  it("falls back to the extension for generic content types", () => {
    expect(getModelFormat("part.stl", "application/octet-stream")).toBe("stl");
    expect(getModelFormat("mesh.obj", "text/plain")).toBe("obj");
  });

  it("ignores charset parameters on the content type", () => {
    expect(getModelFormat("blob", "model/obj; charset=utf-8")).toBe("obj");
  });

  it("returns null for non-model files", () => {
    expect(getModelFormat("app.py")).toBeNull();
    expect(getModelFormat("scene.gltf")).toBeNull();
    expect(getModelFormat("readme.txt", "text/plain")).toBeNull();
    expect(getModelFormat("Dockerfile")).toBeNull();
  });

  it("agrees with isModelFile (detection is exactly getModelFormat !== null)", () => {
    for (const [path, ct] of [
      ["part.stl", null],
      ["blob", "model/3mf"],
      ["app.py", null],
      ["readme.txt", "text/plain"],
    ] as const) {
      expect(isModelFile(path, ct)).toBe(getModelFormat(path, ct) !== null);
    }
  });
});

// ---------------------------------------------------------------------------
// modelViewerTheme — resolved theme → 3D preview appearance (shared by formats)
// ---------------------------------------------------------------------------

describe("modelViewerTheme", () => {
  it("returns distinct backgrounds for light and dark", () => {
    expect(modelViewerTheme("light").background).not.toBe(modelViewerTheme("dark").background);
  });

  it("brightens the lights in dark mode so the mesh stays legible", () => {
    const light = modelViewerTheme("light");
    const dark = modelViewerTheme("dark");
    expect(dark.ambientIntensity).toBeGreaterThan(light.ambientIntensity);
    expect(dark.keyIntensity).toBeGreaterThan(light.keyIntensity);
  });

  it("provides an STL default material color for each mode", () => {
    expect(typeof modelViewerTheme("light").stlMaterial).toBe("number");
    expect(typeof modelViewerTheme("dark").stlMaterial).toBe("number");
  });
});

// ---------------------------------------------------------------------------
// indexToLine
// ---------------------------------------------------------------------------

describe("indexToLine", () => {
  const lines = ["hello", "world", "foo"];
  // Absolute offsets:
  //   line 1: 0–4  ("hello")
  //   \n at 5
  //   line 2: 6–10 ("world")
  //   \n at 11
  //   line 3: 12–14 ("foo")

  it("returns 1 for index at start of first line", () => {
    expect(indexToLine(0, lines)).toBe(1);
  });

  it("returns 1 for index at last char of first line", () => {
    expect(indexToLine(4, lines)).toBe(1);
  });

  it("attributes the \\n between lines to the preceding line (index = line1.length)", () => {
    // The loop condition is `remaining <= rawLines[i].length`, so index 5
    // ("hello".length) satisfies `5 <= 5` on i=0 and returns line 1.
    // The newline itself belongs to the line that precedes it.
    expect(indexToLine(5, lines)).toBe(1);
  });

  it("returns 2 for index at start of second line", () => {
    expect(indexToLine(6, lines)).toBe(2);
  });

  it("returns 3 for index inside last line", () => {
    expect(indexToLine(13, lines)).toBe(3);
  });

  it("clamps to last line when index is beyond EOF", () => {
    expect(indexToLine(999, lines)).toBe(3);
  });

  it("handles single-line file", () => {
    expect(indexToLine(3, ["abcdef"])).toBe(1);
  });

  it("handles empty file (empty lines array)", () => {
    // No lines — returns 0 (rawLines.length = 0).
    expect(indexToLine(0, [])).toBe(0);
  });

  it("handles file with empty lines", () => {
    // ["", "x"] → line 1 is empty (length 0), line 2 starts at offset 1.
    expect(indexToLine(0, ["", "x"])).toBe(1); // on the empty first line
    expect(indexToLine(1, ["", "x"])).toBe(2); // on "x"
  });
});

// ---------------------------------------------------------------------------
// prepareHtmlPreviewDoc — new-tab links; same-page anchors stay in the frame
// ---------------------------------------------------------------------------

describe("prepareHtmlPreviewDoc", () => {
  const HEAD = HTML_PREVIEW_HEAD;
  const BASE = '<base target="_blank">';

  it("injects the head markup, base target first, inside an existing <head>", () => {
    const html = "<!DOCTYPE html><html><head><title>x</title></head><body>hi</body></html>";
    const out = prepareHtmlPreviewDoc(html);
    expect(HEAD.startsWith(BASE)).toBe(true);
    expect(out).toContain(`<head>${HEAD}<title>x</title>`);
    // Doctype stays first so the document keeps standards mode.
    expect(out.indexOf("<!DOCTYPE html>")).toBe(0);
  });

  it("matches <head> with attributes", () => {
    const out = prepareHtmlPreviewDoc('<head lang="en"><meta></head>');
    expect(out).toContain(`<head lang="en">${HEAD}<meta>`);
  });

  it("creates a <head> after <html> when none exists", () => {
    const out = prepareHtmlPreviewDoc("<!DOCTYPE html><html><body>hi</body></html>");
    expect(out).toContain(`<html><head>${HEAD}</head><body>`);
    expect(out.indexOf("<!DOCTYPE html>")).toBe(0);
  });

  it("prepends the head markup for a bare fragment (no doctype to displace)", () => {
    const out = prepareHtmlPreviewDoc('<a href="https://example.com">link</a>');
    expect(out).toBe(`${HEAD}<a href="https://example.com">link</a>`);
  });

  it("is case-insensitive on the HEAD tag", () => {
    const out = prepareHtmlPreviewDoc("<HEAD></HEAD>");
    expect(out).toContain(`<HEAD>${HEAD}`);
  });

  it("preserves an existing <base href>; the injected target tag wins by order", () => {
    // Browsers use the first <base> for each attribute, so injecting our
    // `target` tag ahead of the artifact's keeps its `href` intact while still
    // forcing links to a new tab.
    const html = '<head><base href="https://cdn.example.com/"></head>';
    const out = prepareHtmlPreviewDoc(html);
    expect(out).toBe(`<head>${HEAD}<base href="https://cdn.example.com/"></head>`);
    expect(out.indexOf(BASE)).toBeLessThan(out.indexOf("<base href"));
  });

  it("injects one base tag and one anchor script, and re-preparing adds no second copy", () => {
    const once = prepareHtmlPreviewDoc("<head></head>");
    expect(once.match(/<base target="_blank">/g)).toHaveLength(1);
    expect(once.split("<script>")).toHaveLength(2);
    expect(prepareHtmlPreviewDoc(once)).toBe(once);
  });

  it("still injects a real base when the literal base string only appears in content", () => {
    // Regression: a loose `html.includes(baseTag)` idempotency check wrongly
    // skipped injection for content that merely *mentions* the string (e.g. a
    // comment or code sample), leaving links to navigate the preview in place
    // instead of opening a new tab. The base must still land in <head>.
    const html = '<html><head></head><body><!-- <base target="_blank"> --></body></html>';
    const out = prepareHtmlPreviewDoc(html);
    expect(out).toContain(`<head>${HEAD}</head>`);
  });

  it("skips a <head> literal inside a comment and injects into the real head", () => {
    const out = prepareHtmlPreviewDoc("<!-- <head> --><html><head></head></html>");
    expect(out).toBe(`<!-- <head> --><html><head>${HEAD}</head></html>`);
  });

  it("keeps an artifact script intact when it only mentions <head>/<html> (bare fragment)", () => {
    // The injected markup carries its own </script>; landing inside this string
    // would end the artifact's script early.
    const script = "<script>var t = '<html><head><title>Example</title></head></html>';</script>";
    const out = prepareHtmlPreviewDoc(`${script}<p>hi</p>`);
    expect(out).toBe(`${HEAD}${script}<p>hi</p>`);
  });

  it("recognizes unusual script end tags and comment terminators while scanning", () => {
    const script = "<script>var t = '<head>';</script\t\n bar>";
    expect(prepareHtmlPreviewDoc(`${script}<html><head></head></html>`)).toBe(
      `${script}<html><head>${HEAD}</head></html>`,
    );
    expect(prepareHtmlPreviewDoc("<!-- <head> --!><html><head></head></html>")).toBe(
      `<!-- <head> --!><html><head>${HEAD}</head></html>`,
    );
    // An abruptly closed empty comment ends at once rather than swallowing the document.
    expect(prepareHtmlPreviewDoc("<!--><html><head></head></html>")).toBe(
      `<!--><html><head>${HEAD}</head></html>`,
    );
    expect(prepareHtmlPreviewDoc("<!---><html><head></head></html>")).toBe(
      `<!---><html><head>${HEAD}</head></html>`,
    );
    // A hyphen continues a tag name, so this is script text, not an end tag.
    const fake = "<script>const sample = '</script-not-real><html><head>';</script><p>hi</p>";
    expect(prepareHtmlPreviewDoc(fake)).toBe(`${HEAD}${fake}`);
    const quoted = "<script data-x=\"a>b\">var t = '<head>';</script><p>hi</p>";
    expect(prepareHtmlPreviewDoc(quoted)).toBe(`${HEAD}${quoted}`);
    expect(prepareHtmlPreviewDoc('<html><head data-x="a>b"></head></html>')).toBe(
      `<html><head data-x="a>b">${HEAD}</head></html>`,
    );
  });

  it("scans unterminated and repeated incomplete tags in linear time", () => {
    // An incomplete tag must not cause excessive backtracking, nor be rescanned once the
    // scan has moved past it; each case stays near-instant when progress is linear.
    const cases = [
      `<p>ok</p><script ${'"'.repeat(36)}`,
      '<script "'.repeat(8000),
      "<head '".repeat(8000),
      `<script>${"</script ".repeat(8000)}`,
      `<p>ok</p>${"<script a>".repeat(8000)}`,
    ];
    for (const hostile of cases) {
      const started = performance.now();
      expect(prepareHtmlPreviewDoc(hostile)).toBe(`${HEAD}${hostile}`);
      expect(performance.now() - started).toBeLessThan(500);
    }
  });

  it("does not treat a start tag still open at end of input as the head", () => {
    // The parser drops such a tag, so markup placed after it would vanish; the <html>
    // fallback supplies a real head instead, and a bare open tag gets the prepend.
    expect(prepareHtmlPreviewDoc('<html><head data-x="a>b')).toBe(
      `<html><head>${HEAD}</head><head data-x="a>b`,
    );
    expect(prepareHtmlPreviewDoc("<head ")).toBe(`${HEAD}<head `);
  });

  it("keeps scanning through a double-escaped </script> inside an artifact script", () => {
    // After `<!--<script`, the tokenizer is double-escaped: `</script>` only steps back to the
    // escaped state, so the element (and its `<head>` literal) runs on to the later `</script>`.
    const script = "<script><!--<script></script><head>-->\nwindow.artifactRan = 1;\n</script>";
    const rest = '\n<p id="after">ok</p>';
    expect(prepareHtmlPreviewDoc(`${script}${rest}`)).toBe(`${HEAD}${script}${rest}`);
    // `-->` leaves the escaped states, so the next `</script>` closes the element for real.
    const closed = "<script><!--<script>--></script><html><head></head></html>";
    expect(prepareHtmlPreviewDoc(closed)).toBe(
      `<script><!--<script>--></script><html><head>${HEAD}</head></html>`,
    );
    // In the escaped state alone, `</script>` still closes the element.
    const escapedOnly = "<script><!--</script><html><head></head></html>";
    expect(prepareHtmlPreviewDoc(escapedOnly)).toBe(
      `<script><!--</script><html><head>${HEAD}</head></html>`,
    );
  });

  it("treats a quote as an attribute value delimiter only after =, like the tokenizer", () => {
    // `"a` is an attribute name here, so the tag ends at the first `>` and the markup lands
    // where the parser's head begins.
    expect(prepareHtmlPreviewDoc('<html><head "a>b"></head></html>')).toBe(
      `<html><head "a>${HEAD}b"></head></html>`,
    );
    // Inside an unquoted value, `=` and quotes are plain text too: the tag ends at the first `>`,
    // so the markup stays out of the artifact's script.
    const unquoted = '<head data=x="y><script>window.before="foo>";window.after=1;</script>';
    expect(prepareHtmlPreviewDoc(unquoted)).toBe(
      `<head data=x="y>${HEAD}<script>window.before="foo>";window.after=1;</script>`,
    );
  });

  it("treats an unterminated <script> or comment as swallowing the rest of the document", () => {
    const script = "<script>var t = '<head>';";
    expect(prepareHtmlPreviewDoc(`${script}<html><head></head></html>`)).toBe(
      `${HEAD}${script}<html><head></head></html>`,
    );
    expect(prepareHtmlPreviewDoc("<!-- <head><html>")).toBe(`${HEAD}<!-- <head><html>`);
  });

  it("does not mistake <header> for <head>", () => {
    const out = prepareHtmlPreviewDoc("<header>Title</header><p>hi</p>");
    expect(out).toBe(`${HEAD}<header>Title</header><p>hi</p>`);
  });
});

// ---------------------------------------------------------------------------
// Same-page anchor script — the in-frame click handler prepareHtmlPreviewDoc injects
// ---------------------------------------------------------------------------

describe("prepareHtmlPreviewDoc same-page anchor script", () => {
  const SCRIPT_BODY = HTML_PREVIEW_HEAD.slice(
    HTML_PREVIEW_HEAD.indexOf("<script>") + "<script>".length,
    HTML_PREVIEW_HEAD.lastIndexOf("</script>"),
  );

  /** The capture-phase window listeners the script registers; removed again in afterAll. */
  let registered: [string, EventListener][] = [];

  beforeAll(() => {
    expect(SCRIPT_BODY).not.toBe("");
    const register = vi.spyOn(window, "addEventListener");
    // The handler body is a compile-time constant, so the Function constructor is safe here;
    // vitest's jsdom does not execute inserted <script> elements.
    new Function(SCRIPT_BODY)();
    registered = register.mock.calls
      .filter(([type, , options]) => (type === "click" || type === "auxclick") && options === true)
      .map(([type, listener]) => [type, listener as EventListener]);
    register.mockRestore();
    expect(registered.map(([type]) => type)).toEqual(["click", "auxclick"]);
  });

  afterAll(() => {
    for (const [type, listener] of registered) window.removeEventListener(type, listener, true);
    document.head.innerHTML = "";
  });

  beforeEach(() => {
    // Drop the previous test's base before touching history, which resolves against it.
    document.head.innerHTML = "";
    history.replaceState(null, "", "/preview");
    // Like a srcdoc frame, whose base URL is its embedder's: a bare "#x" resolves to another
    // document, so only the handler navigating this document keeps the click on the page.
    document.head.innerHTML = '<base target="_blank"><base href="https://host.example/app/">';
    document.body.innerHTML = "";
  });

  /** What the injected handler did with an activation. */
  type Outcome = "handled" | "prevented" | "untouched";

  /** Let jsdom's asynchronous navigation and the handler's cleanup task run. */
  const settle = () =>
    new Promise<void>((resolve) => {
      setTimeout(resolve, 0);
    });

  /** Rough stand-in for URL parsing's whitespace handling, enough to tell fragment links apart. */
  const isFragment = (href: string | null): boolean =>
    Array.from(href ?? "")
      .filter((c) => c > " ")
      .join("")
      .startsWith("#");

  /**
   * Dispatch an activation on `target` and report what the handler did. A probe registered
   * before the dispatch runs after every artifact-style listener but before the handler's
   * finisher, which joins window's list during the dispatch: it records whether the page had
   * already cancelled, and cancels non-fragment links itself so jsdom never attempts a real
   * navigation. "handled" means the finisher cancelled the click and navigated the document.
   */
  async function click(
    target: string | Element,
    init: MouseEventInit = {},
    type = "click",
  ): Promise<Outcome> {
    let beforeFinish = false;
    let probeCancelled = false;
    window.addEventListener(
      type,
      (event) => {
        beforeFinish = event.defaultPrevented;
        const anchor = event
          .composedPath()
          .find((node) => node instanceof Element && node.matches("a[href],area[href]"));
        if (
          !beforeFinish &&
          !isFragment((anchor as Element | undefined)?.getAttribute("href") ?? null)
        ) {
          event.preventDefault();
          probeCancelled = true;
        }
      },
      { once: true },
    );
    const element = typeof target === "string" ? document.querySelector(target) : target;
    expect(element).not.toBeNull();
    const event = new MouseEvent(type, {
      bubbles: true,
      cancelable: true,
      composed: true,
      ...init,
    });
    element?.dispatchEvent(event);
    await settle();
    if (beforeFinish) return "prevented";
    if (probeCancelled || !event.defaultPrevented) return "untouched";
    return "handled";
  }

  it("keeps a fragment-only link in the frame by navigating this document", async () => {
    document.body.innerHTML = '<a id="link" href="#section-3">Jump</a><h2 id="section-3">S3</h2>';
    expect(await click("#link")).toBe("handled");
    expect(location.hash).toBe("#section-3");
    expect(location.pathname).toBe("/preview");
    // Nothing in the artifact is modified on the way; the injected base is untouched.
    expect(document.querySelector("base")?.hasAttribute("href")).toBe(false);
  });

  it("keeps modifier clicks in the frame too (a new tab could only show a blank page)", async () => {
    document.body.innerHTML = '<a id="a" href="#one">1</a><a id="b" href="#two">2</a>';
    expect(await click("#a", { ctrlKey: true })).toBe("handled");
    expect(location.hash).toBe("#one");
    expect(await click("#b", { metaKey: true, shiftKey: true })).toBe("handled");
    expect(location.hash).toBe("#two");
  });

  it("handles middle clicks (auxclick) the same way and leaves right clicks alone", async () => {
    document.body.innerHTML = '<a id="link" href="#mid">m</a>';
    expect(await click("#link", { button: 2 }, "auxclick")).toBe("untouched");
    expect(location.hash).toBe("");
    expect(await click("#link", { button: 1 }, "auxclick")).toBe("handled");
    expect(location.hash).toBe("#mid");
  });

  it("keeps a link with its own target in the frame", async () => {
    document.body.innerHTML = '<a id="blank" href="#x" target="_blank">x</a>';
    expect(await click("#blank")).toBe("handled");
    expect(location.hash).toBe("#x");
  });

  it("finds links inside an open shadow root through composedPath", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    root.innerHTML = '<a href="#shadow">s</a>';
    const link = root.querySelector("a");
    expect(link).not.toBeNull();
    expect(await click(link as Element)).toBe("handled");
    expect(location.hash).toBe("#shadow");
    // A light-DOM anchor wrapping a shadow host is found along the composed path too.
    document.body.innerHTML = '<a href="#wrap"><span id="wrapped"></span></a>';
    const inner = (document.getElementById("wrapped") as HTMLElement).attachShadow({
      mode: "open",
    });
    inner.innerHTML = "<b>inside</b>";
    expect(await click(inner.querySelector("b") as Element)).toBe("handled");
    expect(location.hash).toBe("#wrap");
  });

  it("handles clicks on elements nested in the anchor and on <area> hotspots", async () => {
    document.body.innerHTML =
      '<a href="#a"><span id="inner">in</span></a><map><area id="hot" href="#b" shape="default"></map>';
    expect(await click("#inner")).toBe("handled");
    expect(location.hash).toBe("#a");
    expect(await click("#hot")).toBe("handled");
    expect(location.hash).toBe("#b");
  });

  it("lets a document-level handler the page registers later cancel the click first", async () => {
    document.body.innerHTML = '<a id="route" href="#settings">settings</a>';
    const router = (event: Event) => event.preventDefault();
    document.addEventListener("click", router);
    try {
      expect(await click("#route")).toBe("prevented");
      expect(location.hash).toBe("");
    } finally {
      document.removeEventListener("click", router);
    }
  });

  it("runs after listeners the page registers later on window, so they still cancel or route", async () => {
    // The finisher joins window's bubble list during the dispatch and therefore runs last. Until
    // then the event and the document are untouched: `defaultPrevented` is false, relative URLs
    // still resolve against the artifact's own base, and every way of cancelling counts.
    document.body.innerHTML = '<a id="route" href="#settings">settings</a>';
    async function cancelsVia(router: EventListener): Promise<void> {
      window.addEventListener("click", router);
      try {
        expect(await click("#route")).toBe("prevented");
        expect(location.hash).toBe("");
      } finally {
        window.removeEventListener("click", router);
      }
    }
    await cancelsVia((event) => event.preventDefault());
    await cancelsVia((event) => {
      event.returnValue = false;
    });
    let routed = 0;
    await cancelsVia((event) => {
      if (event.defaultPrevented) return;
      routed++;
      event.preventDefault();
    });
    expect(routed).toBe(1);
    let resolved = "";
    await cancelsVia((event) => {
      resolved = new URL("child.js", document.baseURI).href;
      event.preventDefault();
    });
    expect(resolved).toBe("https://host.example/app/child.js");
    // `window.onclick = () => false` cancels through the browser's own handler machinery; vitest's
    // `window` global does not forward that accessor to jsdom, so the Playwright scenario in
    // tests/e2e_ui/files/test_html_preview.py covers it in a real browser.
  });

  it("leaves clicks the page handled itself and non-fragment links alone", async () => {
    document.body.innerHTML =
      '<a id="own" href="#section-3">own</a><a id="ext" href="https://example.com/x">ext</a>' +
      '<a id="rel" href="other.html#frag">rel</a><a id="nbsp" href="&#160;#frag">nb</a>' +
      '<h2 id="section-3">S3</h2>';
    document.getElementById("own")?.addEventListener("click", (event) => event.preventDefault());
    expect(await click("#own")).toBe("prevented");
    expect(location.hash).toBe("");
    expect(await click("#ext")).toBe("untouched");
    expect(await click("#rel")).toBe("untouched");
    // A non-breaking space survives URL parsing, so this is a relative path, not a fragment.
    expect(await click("#nbsp")).toBe("untouched");
    expect(location.hash).toBe("");
  });

  it("drops only the ASCII whitespace URL parsing drops before classifying the href", async () => {
    // Tab and newline go anywhere; spaces and other C0 controls only at the ends.
    document.body.innerHTML =
      '<a id="spaced-link" href=" \t#spa\nced ">s</a><h2 id="spaced">S</h2>';
    expect(await click("#spaced-link")).toBe("handled");
    expect(location.hash).toBe("#spaced");
  });
});

// ---------------------------------------------------------------------------
// openHtmlArtifactInNewTab — pop-out renders in an isolated sandboxed iframe
// ---------------------------------------------------------------------------

describe("openHtmlArtifactInNewTab", () => {
  it("renders the artifact in a sandboxed, opaque-origin iframe (never the app origin)", () => {
    // A real (detached) document stands in for the popped tab's document.
    const shellDoc = document.implementation.createHTMLDocument("");
    const open = vi.fn(() => ({ document: shellDoc }) as unknown as Window);

    const ok = openHtmlArtifactInNewTab("<h1>hi</h1>", "art.html", { open });

    expect(ok).toBe(true);
    // Critically: the artifact is NOT navigated to as a top-level blob:/data:
    // page (which would inherit the app origin) — it's hosted in about:blank.
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    const frame = shellDoc.querySelector("iframe");
    expect(frame).not.toBeNull();
    const sandbox = frame!.getAttribute("sandbox") ?? "";
    expect(sandbox).toBe(HTML_PREVIEW_SANDBOX);
    // Security invariant: the artifact must never share the app's origin.
    expect(sandbox).not.toContain("allow-same-origin");
    // Links still open in a new tab inside the pop-out (#777).
    expect(frame!.getAttribute("srcdoc")).toContain('<base target="_blank">');
  });

  it("returns false when the popup is blocked (window.open → null)", () => {
    const open = vi.fn(() => null);
    expect(openHtmlArtifactInNewTab("<h1>hi</h1>", "art.html", { open })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// lineOverlapsSelection
// ---------------------------------------------------------------------------

describe("lineOverlapsSelection", () => {
  // lines: ["ab", "cd", "ef"]
  // line 0 ("ab"): chars 0–1
  // line 1 ("cd"): chars 3–4
  // line 2 ("ef"): chars 6–7
  const lines = ["ab", "cd", "ef"];

  it("returns true when selection fully covers a line", () => {
    expect(lineOverlapsSelection(0, lines, 0, 8)).toBe(true);
  });

  it("returns true when selection starts and ends on the same line", () => {
    expect(lineOverlapsSelection(0, lines, 0, 2)).toBe(true);
  });

  it("returns true when selection spans from line 0 into line 1", () => {
    expect(lineOverlapsSelection(1, lines, 1, 4)).toBe(true);
  });

  it("returns false when selection ends exactly at the start of the line (exclusive end)", () => {
    // line 1 starts at offset 3; selection end_index=3 means end is exclusive
    expect(lineOverlapsSelection(1, lines, 0, 3)).toBe(false);
  });

  it("returns false when selection is entirely before the line", () => {
    expect(lineOverlapsSelection(2, lines, 0, 2)).toBe(false);
  });

  it("returns false when selection starts strictly after the line (past the \\n)", () => {
    // line 0 ("ab") has lineEnd = 2. The \\n at offset 2 is included in line 0's
    // range (start <= lineEnd), so to be strictly after we need start = 3.
    expect(lineOverlapsSelection(0, lines, 3, 5)).toBe(false);
  });

  it("returns true for a single-character selection touching a line", () => {
    expect(lineOverlapsSelection(1, lines, 3, 4)).toBe(true);
  });

  it("handles a selection spanning all lines", () => {
    expect(lineOverlapsSelection(0, lines, 0, 8)).toBe(true);
    expect(lineOverlapsSelection(1, lines, 0, 8)).toBe(true);
    expect(lineOverlapsSelection(2, lines, 0, 8)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// getSelectionOffsets — DOM Range → absolute byte offsets
// ---------------------------------------------------------------------------

describe("getSelectionOffsets", () => {
  // Build a code container whose children carry `data-line` (1-based) and
  // hold a single text node — mirroring the highlighted line elements
  // CodeViewer renders. jsdom supports Range over these nodes.
  function buildContainer(rawLines: string[]): HTMLElement {
    const container = document.createElement("div");
    rawLines.forEach((line, i) => {
      const el = document.createElement("div");
      el.dataset.line = String(i + 1);
      el.textContent = line;
      container.appendChild(el);
    });
    document.body.appendChild(container);
    return container;
  }

  function lineTextNode(container: HTMLElement, lineIdx: number): Text {
    return container.children[lineIdx].firstChild as Text;
  }

  it("computes absolute offsets for a single-line selection", () => {
    // WHY: the common case — selecting chars within one line must map column
    // offsets onto the absolute index, exercising the preRange column math.
    const rawLines = ["hello", "world", "foo"];
    const container = buildContainer(rawLines);
    const range = document.createRange();
    // Select "ell" on line 1 (cols 1..4).
    range.setStart(lineTextNode(container, 0), 1);
    range.setEnd(lineTextNode(container, 0), 4);

    expect(getSelectionOffsets(range, container, rawLines)).toEqual({
      start_index: 1,
      end_index: 4,
    });
    container.remove();
  });

  it("sums preceding line lengths (+1 per newline) for a multi-line selection", () => {
    // WHY: spanning lines must add each prior line's length plus its \n, so a
    // boundary on line 2 lands past the line-1 text and its newline.
    const rawLines = ["hello", "world", "foo"];
    const container = buildContainer(rawLines);
    const range = document.createRange();
    // Start at col 2 of line 1, end at col 3 of line 2.
    range.setStart(lineTextNode(container, 0), 2);
    range.setEnd(lineTextNode(container, 1), 3);

    // start = 2; end = ("hello".length 5 + \n 1) + 3 = 9.
    expect(getSelectionOffsets(range, container, rawLines)).toEqual({
      start_index: 2,
      end_index: 9,
    });
    container.remove();
  });

  it("returns null when a boundary is outside any data-line element", () => {
    // WHY: a selection that escaped the code container (e.g. into the gutter)
    // can't be resolved to a line, so the helper must bail rather than emit a
    // bogus offset.
    const rawLines = ["hello"];
    const container = buildContainer(rawLines);
    const stray = document.createElement("span");
    stray.textContent = "outside";
    document.body.appendChild(stray);

    const range = document.createRange();
    range.setStart(stray.firstChild as Text, 0);
    range.setEnd(stray.firstChild as Text, 3);

    expect(getSelectionOffsets(range, container, rawLines)).toBeNull();
    container.remove();
    stray.remove();
  });

  it("returns null when a line element has a zero/missing line number", () => {
    // WHY: data-line="0" parses to a falsy line number; the guard rejects it
    // rather than computing against a non-existent line 0.
    const container = document.createElement("div");
    const el = document.createElement("div");
    el.dataset.line = "0";
    el.textContent = "abc";
    container.appendChild(el);
    document.body.appendChild(container);

    const range = document.createRange();
    range.setStart(el.firstChild as Text, 0);
    range.setEnd(el.firstChild as Text, 2);

    expect(getSelectionOffsets(range, container, ["abc"])).toBeNull();
    container.remove();
  });
});
