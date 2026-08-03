/**
 * LLMs often emit TeX delimiters (`\(...\)` and `\[...\]`), while remark-math
 * parses dollar delimiters. Convert them to `$`/`$$`, but only where doing so
 * is safe:
 *
 * - Not inside fenced or inline code, so code examples stay verbatim.
 * - Not inside an already `$`/`$$`-delimited math span, so a LaTeX line break
 *   (`\\[1em]`) inside `$$\begin{aligned}…\end{aligned}$$` isn't mistaken for a
 *   display-math opener and turned into `\$$1em]`.
 * - A literal `\\` (escaped backslash / LaTeX line break) is copied verbatim so
 *   its trailing `\[`/`\(` isn't read as an explicit delimiter.
 */
export function normalizeExplicitMathDelimiters(text: string): string {
  let result = "";
  let inFence = false;
  // Length of the backtick run that opened the current inline-code span, or 0
  // when not in inline code. Tracking the run length lets a ``…`` span close
  // only on a matching-length run, so single backticks inside it don't leak.
  let inlineCodeTicks = 0;
  // Inside a pre-existing `$…$` / `$$…$$` span (toggled on each run of `$`).
  let inMath = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const atLineStart = i === 0 || text[i - 1] === "\n";

    if (
      !inlineCodeTicks &&
      atLineStart &&
      (text.startsWith("```", i) || text.startsWith("~~~", i))
    ) {
      inFence = !inFence;
      result += text.slice(i, i + 3);
      i += 2;
      continue;
    }
    if (inFence) {
      result += char;
      continue;
    }

    if (char === "`") {
      let run = 1;
      while (text[i + run] === "`") run += 1;
      if (inlineCodeTicks === 0) {
        inlineCodeTicks = run;
      } else if (run === inlineCodeTicks) {
        inlineCodeTicks = 0;
      }
      result += text.slice(i, i + run);
      i += run - 1;
      continue;
    }
    if (inlineCodeTicks) {
      result += char;
      continue;
    }

    if (char === "\\" && text[i + 1] === "\\") {
      result += "\\\\";
      i += 1;
      continue;
    }

    if (char === "$") {
      let run = 1;
      while (text[i + run] === "$") run += 1;
      inMath = !inMath;
      result += text.slice(i, i + run);
      i += run - 1;
      continue;
    }

    if (!inMath) {
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
