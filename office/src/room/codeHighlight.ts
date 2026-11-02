// Syntax highlighting for the file viewer. Imported lazily, so Shiki and its
// grammars load only when a file is opened.

import { type BundledLanguage, type BundledTheme, type HighlighterGeneric, bundledLanguagesInfo, createHighlighter } from "shiki";
import { createJavaScriptRegexEngine } from "shiki/engine/javascript";

const THEME: BundledTheme = "github-dark-dimmed";

// Files whose name, not extension, says what they are.
const NAMED_FILES: Record<string, string> = {
  dockerfile: "docker",
  makefile: "make",
  justfile: "just",
  ".gitignore": "ignore",
  ".dockerignore": "ignore",
  ".env": "dotenv",
  "cmakelists.txt": "cmake",
};

// Extensions Shiki does not list as a language id or alias.
const EXTENSIONS: Record<string, string> = {
  mjs: "javascript",
  cjs: "javascript",
  mts: "typescript",
  cts: "typescript",
  htm: "html",
  yml: "yaml",
  txt: "text",
  lock: "text",
  log: "log",
};

// Past this size highlighting is slow enough to freeze the page, so the file shows plain.
export const MAX_HIGHLIGHT_CHARS = 300_000;

const KNOWN = new Map<string, string>();
for (const language of bundledLanguagesInfo) {
  KNOWN.set(language.id, language.id);
  for (const alias of language.aliases ?? []) KNOWN.set(alias, language.id);
}

/** The Shiki language for a file path; "text" when none fits. */
export function languageForFile(path: string): string {
  const name = path.split("/").pop()?.toLowerCase() ?? "";
  const named = NAMED_FILES[name] ?? (name.startsWith(".env.") ? "dotenv" : undefined);
  if (named && KNOWN.has(named)) return KNOWN.get(named) as string;
  const extension = name.includes(".") ? name.slice(name.lastIndexOf(".") + 1) : "";
  const mapped = EXTENSIONS[extension] ?? extension;
  if (mapped === "text") return "text";
  return KNOWN.get(mapped) ?? "text";
}

// The JavaScript regex engine needs no WebAssembly, which the office's content policy blocks.
let highlighter: Promise<HighlighterGeneric<BundledLanguage, BundledTheme>> | null = null;

function getHighlighter() {
  highlighter ??= createHighlighter({ themes: [THEME], langs: [], engine: createJavaScriptRegexEngine() });
  return highlighter;
}

export async function highlightFile(code: string, path: string): Promise<{ html: string; language: string }> {
  const language = code.length > MAX_HIGHLIGHT_CHARS ? "text" : languageForFile(path);
  const shiki = await getHighlighter();
  // Grammars load on first use, so only the languages actually opened are fetched.
  if (language !== "text" && !shiki.getLoadedLanguages().includes(language)) await shiki.loadLanguage(language as BundledLanguage);
  return { html: shiki.codeToHtml(code, { lang: language, theme: THEME }), language };
}
