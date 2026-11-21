interface MarkdownNode {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: MarkdownNode[];
}

const PROSE_TAGS = new Set([
  "p",
  "h1",
  "h2",
  "h3",
  "h4",
  "h5",
  "h6",
  "ul",
  "ol",
  "li",
  "blockquote",
  "th",
  "td",
  "a",
]);

/** Let each prose block choose its direction, independently of adjacent blocks. */
export function rehypeMessageDirection() {
  function visit(node: MarkdownNode): void {
    if (node.type === "element") {
      if (node.tagName === "pre" || node.tagName === "code") {
        node.properties = { ...node.properties, dir: "ltr" };
        return;
      }
      if (PROSE_TAGS.has(node.tagName ?? "")) {
        node.properties = { ...node.properties, dir: node.properties?.dir ?? "auto" };
      }
    }
    for (const child of node.children ?? []) visit(child);
  }
  return visit;
}
