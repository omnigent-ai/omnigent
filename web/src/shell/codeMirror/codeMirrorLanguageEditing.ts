// Language-aware editing without a CodeMirror parser: the viewer highlights
// with Shiki, so comment tokens and bracket pairs come from this table, keyed
// by the file's language as detected from its name.

import type { CommentTokens } from "@codemirror/commands";
import { EditorState, type Extension } from "@codemirror/state";
import { detectLang } from "../codeViewerHelpers";

const C_LIKE: CommentTokens = { line: "//", block: { open: "/*", close: "*/" } };
const HASH: CommentTokens = { line: "#" };
const DOUBLE_DASH: CommentTokens = { line: "--" };
const MARKUP: CommentTokens = { block: { open: "<!--", close: "-->" } };
const CSS: CommentTokens = { block: { open: "/*", close: "*/" } };

const COMMENT_TOKENS: Readonly<Record<string, CommentTokens>> = {
  ...Object.fromEntries(
    [
      "c",
      "cpp",
      "csharp",
      "dart",
      "go",
      "groovy",
      "java",
      "javascript",
      "js",
      "jsonc",
      "json5",
      "jsx",
      "kotlin",
      "objective-c",
      "php",
      "proto",
      "rust",
      "scala",
      "scss",
      "less",
      "swift",
      "ts",
      "tsx",
      "typescript",
      "zig",
    ].map((language) => [language, C_LIKE]),
  ),
  ...Object.fromEntries(
    [
      "bash",
      "dockerfile",
      "cmake",
      "elixir",
      "fish",
      "graphql",
      "hcl",
      "julia",
      "make",
      "makefile",
      "nix",
      "perl",
      "powershell",
      "python",
      "r",
      "ruby",
      "sh",
      "shellscript",
      "starlark",
      "terraform",
      "toml",
      "yaml",
      "yml",
      "zsh",
    ].map((language) => [language, HASH]),
  ),
  ...Object.fromEntries(
    ["haskell", "lua", "sql", "plsql"].map((language) => [language, DOUBLE_DASH]),
  ),
  ...Object.fromEntries(
    ["astro", "html", "markdown", "md", "mdx", "svelte", "vue", "xml", "svg"].map((language) => [
      language,
      MARKUP,
    ]),
  ),
  css: CSS,
  clojure: { line: ";" },
  lisp: { line: ";" },
  scheme: { line: ";" },
  erlang: { line: "%" },
  latex: { line: "%" },
  tex: { line: "%" },
  matlab: { line: "%" },
  vim: { line: '"' },
  ini: { line: ";" },
  ocaml: { block: { open: "(*", close: "*)" } },
  bat: { line: "REM" },
};

/** Languages with no comment syntax, so comment toggling leaves them alone. */
export const UNCOMMENTED_LANGUAGES: ReadonlySet<string> = new Set(["json", "csv", "diff", "text"]);

// Highlighted as JSON, but these JSON dialects allow comments.
const EDITING_LANGUAGE_BY_EXTENSION: Readonly<Record<string, string>> = {
  jsonc: "jsonc",
  json5: "json5",
};

/** The language whose comments and brackets editing `path` uses. */
export function editingLanguageFor(path: string): string {
  const extension = path.split(".").pop()?.toLowerCase() ?? "";
  return EDITING_LANGUAGE_BY_EXTENSION[extension] ?? detectLang(path);
}

// Plain text and prose have no brackets worth closing.
const NO_AUTO_CLOSE = new Set(["text", "markdown", "md", "mdx", "plaintext"]);

/** Comment tokens for a Shiki language id, or undefined when it has no comments. */
export function commentTokensFor(language: string): CommentTokens | undefined {
  return COMMENT_TOKENS[language];
}

/** Whether typing an opening bracket or quote should insert its closing pair. */
export function closesBracketsFor(language: string): boolean {
  return !NO_AUTO_CLOSE.has(language);
}

/** Language data that comment toggling and bracket closing read for the file at `path`. */
export function languageEditing(path: string): Extension {
  const language = editingLanguageFor(path);
  const commentTokens = commentTokensFor(language);
  const closeBrackets = closesBracketsFor(language) ? undefined : { brackets: [] };
  if (!commentTokens && !closeBrackets) return [];
  return EditorState.languageData.of(() => [
    {
      ...(commentTokens && { commentTokens }),
      ...(closeBrackets && { closeBrackets }),
    },
  ]);
}
