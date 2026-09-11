/** Format JSON for reading without rewriting stored source, hashes, or drafts. */
export function prettyJSON(text) {
  try {
    const value = JSON.parse(text);
    if (value === null || typeof value !== "object") return text;
  } catch {
    return text;
  }
  // Preserve number literals and duplicate keys; parse/stringify can alter large IDs.
  const tokens = text.match(/"(?:\\.|[^"\\])*"|[^\s{}[\],:]+|[{}[\],:]/g) || [];
  let result = "",
    depth = 0;
  const empty = [];
  for (let i = 0; i < tokens.length; i++) {
    const token = tokens[i];
    if (token === "{" || token === "[") {
      const isEmpty = tokens[i + 1] === (token === "{" ? "}" : "]");
      empty.push(isEmpty);
      result += token;
      if (!isEmpty) result += "\n" + "  ".repeat(++depth);
    } else if (token === "}" || token === "]") {
      if (!empty.pop()) result += "\n" + "  ".repeat(--depth);
      result += token;
    } else if (token === ",") result += ",\n" + "  ".repeat(depth);
    else if (token === ":") result += ": ";
    else result += token;
  }
  return result;
}
