// Preserve case for Replace: the replacement takes the case of the text it
// replaces, as in other code editors. All caps stays all caps, lower stays
// lower, a capitalised word stays capitalised, and hyphen- or underscore-joined
// parts are matched part by part.

function hasUppercase(text: string): boolean {
  return text.toLowerCase() !== text;
}

/** Whether `matched` and `replacement` both join the same number of parts with `separator`. */
function joinsPartsWith(matched: string, replacement: string, separator: string): boolean {
  return (
    matched.includes(separator) &&
    replacement.includes(separator) &&
    matched.split(separator).length === replacement.split(separator).length
  );
}

/** `replacement` in the case of `matched`; unchanged when `matched` is empty. */
export function withCaseOf(matched: string, replacement: string): string {
  if (matched === "" || replacement === "") return replacement;
  const hyphens = joinsPartsWith(matched, replacement, "-");
  const underscores = joinsPartsWith(matched, replacement, "_");
  if (hyphens !== underscores) {
    const separator = hyphens ? "-" : "_";
    const matchedParts = matched.split(separator);
    return replacement
      .split(separator)
      .map((part, index) => withCaseOf(matchedParts[index] ?? "", part))
      .join(separator);
  }
  if (matched.toUpperCase() === matched) return replacement.toUpperCase();
  if (matched.toLowerCase() === matched) return replacement.toLowerCase();
  const first = matched[0] ?? "";
  if (hasUppercase(first)) return replacement[0]!.toUpperCase() + replacement.slice(1);
  if (first.toUpperCase() !== first) return replacement[0]!.toLowerCase() + replacement.slice(1);
  return replacement;
}
