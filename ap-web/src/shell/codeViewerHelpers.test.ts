import { describe, expect, it } from "vitest";
import {
  detectLang,
  getSelectionOffsets,
  indexToLine,
  isBinaryPath,
  lineOverlapsSelection,
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
