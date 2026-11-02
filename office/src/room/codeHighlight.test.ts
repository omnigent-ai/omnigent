import { describe, expect, it } from "vitest";
import { MAX_HIGHLIGHT_CHARS, highlightFile, languageForFile } from "./codeHighlight.ts";

describe("languageForFile", () => {
  it.each([
    ["src/main.js", "javascript"],
    ["server/http.ts", "typescript"],
    ["src/App.tsx", "tsx"],
    ["scripts/build.mjs", "javascript"],
    ["app.py", "python"],
    ["config.yml", "yaml"],
    ["README.md", "markdown"],
    ["style.css", "css"],
    ["index.html", "html"],
    ["deploy.sh", "shellscript"],
    ["Dockerfile", "docker"],
    ["Makefile", "make"],
    ["notes.txt", "text"],
    ["LICENSE", "text"],
    ["data.unknownext", "text"],
  ])("%s is %s", (path, language) => {
    expect(languageForFile(path)).toBe(language);
  });
});

describe("highlightFile", () => {
  it("highlights known languages and escapes the file's text", async () => {
    const { html, language } = await highlightFile('const tag = "<script>";\n', "a.ts");
    expect(language).toBe("typescript");
    expect(html).toContain('class="line"');
    expect(html).not.toContain("<script>");
  });

  it("shows very large files as plain text", async () => {
    expect((await highlightFile("x".repeat(MAX_HIGHLIGHT_CHARS + 1), "big.ts")).language).toBe("text");
  });
});
