// Where a link in a Markdown file leads when the PO clicks it in reader mode.

export type MarkdownLink =
  | { kind: "file"; path: string }
  | { kind: "external"; href: string }
  | { kind: "anchor"; id: string }
  | { kind: "blocked" };

/**
 * A relative link opens that file of the room's folder as a tab; http(s) and mailto
 * links open outside the office. Anything else, or a path leaving the folder, is blocked.
 */
export function resolveMarkdownLink(currentPath: string, href: string): MarkdownLink {
  const target = href.trim();
  if (target.startsWith("#")) return { kind: "anchor", id: decodeURIComponent(target.slice(1)) };
  if (/^(https?:|mailto:)/i.test(target)) return { kind: "external", href: target };
  if (/^[a-z][a-z0-9+.-]*:/i.test(target) || target.startsWith("//")) return { kind: "blocked" };
  const withoutFragment = target.split(/[?#]/)[0];
  if (!withoutFragment) return { kind: "blocked" };
  const base = withoutFragment.startsWith("/") ? [] : currentPath.split("/").slice(0, -1);
  const parts = [...base];
  for (const segment of decodeURIComponent(withoutFragment).split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      if (parts.length === 0) return { kind: "blocked" };
      parts.pop();
    } else parts.push(segment);
  }
  return parts.length ? { kind: "file", path: parts.join("/") } : { kind: "blocked" };
}

/** Markdown files open in reader mode. */
export function isMarkdownFile(path: string): boolean {
  return /\.(md|markdown|mdx)$/i.test(path);
}
