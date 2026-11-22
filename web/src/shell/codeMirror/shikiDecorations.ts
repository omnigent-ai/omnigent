// Shiki tokens as CodeMirror mark decorations. Tokens carry the light color
// inline plus `--shiki-dark*` variables, so a theme switch is pure CSS.

import type { Range } from "@codemirror/state";
import { Decoration, type DecorationSet } from "@codemirror/view";
import type { ThemedToken } from "shiki";
import { getHighlighter, type CodeLanguage } from "@/components/ai-elements/code-block";
import { SYNTAX_THEME_NAMES } from "@/lib/syntaxTheme";

/**
 * Upper bound for one tokenization pass. Shiki and decoration building are
 * synchronous, and dense source yields several decorations per line.
 */
export const MAX_HIGHLIGHT_CHARACTERS = 128 * 1024;

/** Class on every token mark; the dark theme swaps in `--shiki-dark` through it. */
export const SHIKI_TOKEN_CLASS = "cm-shiki-token";

/**
 * Builds decorations for tokens of a slice that starts at `baseOffset` in the document.
 *
 * @param tokens Lines of tokens whose `offset` is relative to the slice.
 * @param baseOffset Document position of the slice's first character.
 */
export function tokenDecorations(tokens: ThemedToken[][], baseOffset: number): DecorationSet {
  const ranges: Range<Decoration>[] = [];
  for (const line of tokens) {
    for (const token of line) {
      if (token.content.length === 0) continue;
      const style = tokenStyle(token);
      if (!style) continue;
      ranges.push(
        Decoration.mark({ class: SHIKI_TOKEN_CLASS, attributes: { style } }).range(
          baseOffset + token.offset,
          baseOffset + token.offset + token.content.length,
        ),
      );
    }
  }
  return Decoration.set(ranges, true);
}

/**
 * Tokenizes `text` with the shared Shiki highlighter.
 *
 * @returns No decorations for plain text, empty or oversized input, or a language Shiki can't load.
 */
export async function shikiDecorations(
  text: string,
  language: CodeLanguage,
  baseOffset: number,
): Promise<DecorationSet> {
  if (language === "text" || text.length === 0 || text.length > MAX_HIGHLIGHT_CHARACTERS) {
    return Decoration.none;
  }
  const highlighter = await getHighlighter(language);
  if (!highlighter.getLoadedLanguages().includes(language)) return Decoration.none;
  const result = highlighter.codeToTokens(text, {
    lang: language,
    themes: SYNTAX_THEME_NAMES,
  });
  return tokenDecorations(result.tokens, baseOffset);
}

// Shiki encodes font decorations as bit flags on token.fontStyle.
const SHIKI_ITALIC = 1;
const SHIKI_BOLD = 2;
const SHIKI_UNDERLINE = 4;

function tokenStyle(token: ThemedToken): string {
  const declarations: string[] = [];
  if (token.color) declarations.push(`color:${token.color}`);
  const fontStyle = token.fontStyle ?? 0;
  if (fontStyle > 0) {
    if (fontStyle & SHIKI_ITALIC) declarations.push("font-style:italic");
    if (fontStyle & SHIKI_BOLD) declarations.push("font-weight:600");
    if (fontStyle & SHIKI_UNDERLINE) declarations.push("text-decoration:underline");
  }
  for (const [property, value] of Object.entries(token.htmlStyle ?? {})) {
    declarations.push(`${property}:${value}`);
  }
  return declarations.join(";");
}
