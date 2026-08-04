// A lone `$` reads as a shell-style variable reference — `$VAR_NAME` or
// `${VAR_NAME}` — when followed by a SCREAMING_CASE identifier (an env-var
// convention), so it's treated as literal text rather than a math opener. The
// braces already disambiguate `${A}`, so a single char is enough there; the
// bare form requires 2+ chars so a lone `$X …` still reads as inline math.
// The bare form also demands a full-token boundary (`(?![A-Za-z0-9_])`) so a
// mixed token like `$FOOBar$` isn't matched by its uppercase prefix — the
// lookahead rejects any continuing identifier char so greedy backtracking
// can't settle on `$FOO`; escaping only the opener would strand the closing
// `$` and break genuine inline math.
// Anchored at the `$`, which the caller has already matched.
const SHELL_VAR_RE = /^\$(?:\{[A-Z_][A-Z0-9_]*\}|[A-Z_][A-Z0-9_]+(?![A-Za-z0-9_]))/;

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
 * A single `$` reads as literal prose rather than an inline-math opener when it
 * looks like currency ($5) or a shell-style variable reference ($LLM_API_KEY,
 * ${OMNIGENT_LLM_API_KEY}), so it's escaped and doesn't flip the math span.
 * Otherwise, with single-dollar math enabled, prose like "it costs $5 or $10"
 * or an error such as "Unresolved variable '$LLM_API_KEY' … Set $LLM_API_KEY"
 * renders as a garbled formula. (Genuine inline math that starts with a digit
 * (`$3x$`) or a SCREAMING_CASE identifier (`$N_A$`) is the rare casualty;
 * currency and env-var references in prose are far more common.)
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
      const isLiteralDollar =
        run === 1 && !inMath && (/\d/.test(text[i + 1] ?? "") || SHELL_VAR_RE.test(text.slice(i)));
      if (isLiteralDollar) {
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
