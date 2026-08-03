// Optional 1–3 space indent + a fence run, per CommonMark. Matching the full
// run (not just the first three chars) also keeps a ````-fenced block from
// leaking its fourth backtick into inline-code tracking.
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

/**
 * LLMs often emit TeX delimiters (`\(...\)` and `\[...\]`), while remark-math
 * parses dollar delimiters. Convert them to `$`/`$$`, but only where doing so
 * is safe:
 *
 * - Not inside fenced or inline code, so code examples stay verbatim.
 * - Not inside an already `$`/`$$`-delimited math span, so a LaTeX line break
 *   (`\\[1em]`) inside `$$\begin{aligned}…\end{aligned}$$` isn't mistaken for a
 *   display-math opener and turned into `\$$1em]`.
 * - A literal `\\` or `\$` is copied verbatim so its trailing `\[`/`\(` isn't
 *   read as an explicit delimiter and an escaped dollar isn't re-toggled.
 *
 * A single `$` immediately before a digit reads as currency ($5), not an inline
 * math opener, so it's escaped and doesn't flip the math span — otherwise, with
 * single-dollar math enabled, prose like "it costs $5 or $10" renders as a
 * garbled formula. (Genuine inline math that starts with a digit, e.g. `$3x$`,
 * is the rare casualty; digit-led currency in prose is far more common.)
 */
export function normalizeExplicitMathDelimiters(text: string): string {
  let result = "";
  // The marker (`` ``` `` / `~~~` run) that opened the current fenced block, or
  // "" when outside a fence. CommonMark closes a fence only with the same char
  // and a run at least as long, so a stray `~~~` inside a ``` block stays code.
  let openFence = "";
  // Length of the backtick run that opened the current inline-code span, or 0
  // when not in inline code. Tracking the run length lets a ``…`` span close
  // only on a matching-length run, so single backticks inside it don't leak.
  let inlineCodeTicks = 0;
  // Inside a pre-existing `$…$` / `$$…$$` span (toggled on each run of `$`).
  let inMath = false;

  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const atLineStart = i === 0 || text[i - 1] === "\n";

    if (!inlineCodeTicks && atLineStart) {
      const fence = text.slice(i).match(FENCE_RE);
      if (fence) {
        const marker = fence[1];
        if (!openFence) {
          openFence = marker;
        } else if (marker[0] === openFence[0] && marker.length >= openFence.length) {
          openFence = "";
        }
        result += fence[0];
        i += fence[0].length - 1;
        continue;
      }
    }
    if (openFence) {
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

    if (char === "\\" && (text[i + 1] === "\\" || text[i + 1] === "$")) {
      result += text.slice(i, i + 2);
      i += 1;
      continue;
    }

    if (char === "$") {
      let run = 1;
      while (text[i + run] === "$") run += 1;
      const isCurrency = run === 1 && !inMath && /\d/.test(text[i + 1] ?? "");
      if (isCurrency) {
        result += "\\$";
        continue;
      }
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
