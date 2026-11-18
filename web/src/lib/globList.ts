// Splits the comma-separated glob lists the file search fields take, exactly
// as the runner's `split_glob_list` does, so every search backend sees the
// same patterns.

// The characters Python's `str.strip()` removes (`str.isspace()`), which
// differ from JavaScript's `trim()`: it keeps U+FEFF and removes U+001C-U+001F
// and U+0085.
const PYTHON_WHITESPACE = new Set([
  ..."\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680",
  ..."\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a",
  ..."\u2028\u2029\u202f\u205f\u3000",
]);

function stripLikePython(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && PYTHON_WHITESPACE.has(text[start]!)) start += 1;
  while (end > start && PYTHON_WHITESPACE.has(text[end - 1]!)) end -= 1;
  return text.slice(start, end);
}

/**
 * Split a comma-separated glob list on top-level commas only.
 *
 * Commas inside a `{...}` alternation stay, so `"*.{js,ts}, src/**"` gives
 * `["*.{js,ts}", "src/**"]`. Entries are stripped as Python strips them, and
 * blank ones dropped.
 */
export function splitGlobList(raw: string): string[] {
  const patterns: string[] = [];
  let current = "";
  let depth = 0;
  for (const character of raw) {
    if (character === "{") depth += 1;
    else if (character === "}") depth = Math.max(0, depth - 1);
    else if (character === "," && depth === 0) {
      patterns.push(current);
      current = "";
      continue;
    }
    current += character;
  }
  patterns.push(current);
  return patterns.map(stripLikePython).filter((pattern) => pattern !== "");
}
