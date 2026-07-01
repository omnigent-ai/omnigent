/**
 * LLMs often emit TeX delimiters (`\(...\)` and `\[...\]`), while remark-math
 * parses dollar delimiters. Normalize only outside code so formulas render
 * without corrupting fenced snippets or inline code examples.
 */
export function normalizeExplicitMathDelimiters(text: string): string {
  let result = "";
  let inFence = false;
  let inInlineCode = false;

  for (let i = 0; i < text.length; i += 1) {
    const atLineStart = i === 0 || text[i - 1] === "\n";
    if (!inInlineCode && atLineStart && (text.startsWith("```", i) || text.startsWith("~~~", i))) {
      inFence = !inFence;
      result += text.slice(i, i + 3);
      i += 2;
      continue;
    }

    const char = text[i];
    if (!inFence && char === "`") {
      inInlineCode = !inInlineCode;
      result += char;
      continue;
    }

    if (!inFence && !inInlineCode) {
      const pair = text.slice(i, i + 2);
      if (pair === "\\(" || pair === "\\)") {
        result += "$";
        i += 1;
        continue;
      }
      if (pair === "\\[" || pair === "\\]") {
        result += "$$";
        i += 1;
        continue;
      }
    }

    result += char;
  }

  return result;
}
