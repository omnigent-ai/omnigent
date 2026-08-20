// Formatting for elicitation `content_preview` strings.
//
// `content_preview` is a producer-supplied snapshot of the gated
// value, hard-capped server-side (300 chars for the claude-sdk hook,
// 1024 everywhere else), so JSON previews are routinely cut off
// mid-token. Producers emit three shapes:
//
//   - bare JSON — policy ASK, codex elicitations
//   - `ToolName({...})` — claude-native / claude-sdk permission hooks
//     (the cap can also eat the closing paren)
//   - free-form text — everything else
//
// `JSON.parse` + `JSON.stringify(.., 2)` only handles the first shape
// and only when the preview survived the cap intact. For everything
// JSON-shaped that doesn't parse, `reindentJsonish` pretty-prints
// without parsing: a character scan that tracks string/escape state
// and inserts line breaks + indentation around structural tokens. It
// never adds, drops, or reorders content, so truncated previews stay
// verbatim — just readable.

const INDENT = "  ";

/**
 * Best-effort pretty-printer for JSON-shaped text that may be
 * truncated mid-token. Inserts a line break + indent after `{`, `[`,
 * and `,`, and before `}` and `]`, while leaving string contents
 * (including escaped quotes) untouched. Empty containers stay
 * compact (`{}`, `[]`).
 */
function reindentJsonish(text: string): string {
  let out = "";
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      out += ch;
      if (escaped) escaped = false;
      else if (ch === "\\") escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') {
      inString = true;
      out += ch;
      continue;
    }
    if (ch === "{" || ch === "[") {
      const closer = ch === "{" ? "}" : "]";
      if (text[i + 1] === closer) {
        out += ch + closer;
        i += 1;
        continue;
      }
      depth += 1;
      out += ch + "\n" + INDENT.repeat(depth);
      // Eat the single-line separator space (`json.dumps` emits
      // `", "` / `": "`) so the new line doesn't start indented+1.
      while (text[i + 1] === " ") i += 1;
      continue;
    }
    if (ch === "}" || ch === "]") {
      depth = Math.max(0, depth - 1);
      out += "\n" + INDENT.repeat(depth) + ch;
      continue;
    }
    if (ch === ",") {
      out += ",\n" + INDENT.repeat(depth);
      while (text[i + 1] === " ") i += 1;
      continue;
    }
    out += ch;
  }
  return out;
}

/**
 * Pretty-print JSON-shaped text: canonical two-space indent when it
 * parses, string-preserving reindent when it doesn't (truncated).
 */
function formatJsonish(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return reindentJsonish(text);
  }
}

/**
 * Format an elicitation `content_preview` for display. JSON and
 * `ToolName({...})` shapes are pretty-printed (including previews the
 * server cap cut off mid-token); anything else is returned unchanged.
 */
export function formatPreview(raw: string): string {
  const trimmed = raw.trim();
  if (!trimmed) return "";
  if (trimmed[0] === "{" || trimmed[0] === "[") {
    return formatJsonish(trimmed);
  }
  // `ToolName({...})` wrapper. Only rewrite when the body is
  // JSON-shaped — plain text that happens to contain parens (e.g. a
  // question ending in "(or not)") must pass through untouched. The
  // closing paren is optional: the server cap can truncate it away.
  const call = /^([A-Za-z_][\w.-]*)\((.*)$/s.exec(trimmed);
  if (call) {
    const name = call[1];
    let body = call[2];
    const closed = body.endsWith(")");
    if (closed) body = body.slice(0, -1);
    if (body[0] === "{" || body[0] === "[") {
      return `${name}(${formatJsonish(body)}${closed ? ")" : ""}`;
    }
  }
  return raw;
}
