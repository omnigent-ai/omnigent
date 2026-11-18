// Splits the comma-separated glob lists the file search fields take, exactly
// as the runner's `split_glob_list` does, so every search backend sees the
// same patterns.

/**
 * Split a comma-separated glob list on top-level commas only.
 *
 * Commas inside a `{...}` alternation stay, so `"*.{js,ts}, src/**"` gives
 * `["*.{js,ts}", "src/**"]`. Entries are trimmed and blank ones dropped.
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
  return patterns.map((pattern) => pattern.trim()).filter((pattern) => pattern !== "");
}
