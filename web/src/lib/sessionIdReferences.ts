// A markdown link whose title starts with this names a bare session id the
// message mentioned; the renderer turns it into a session pill once that
// session resolves, and shows the text as written otherwise.
const SESSION_ID_REFERENCE_TITLE_PREFIX = "omnigent-session-id:";

// The server's session id form (32 lowercase hex chars), standing on its own.
// Ids inside paths, URLs, file names or longer tokens (`/c/<id>`, `conv_<id>`,
// `<id>.json`, a 40-char git sha) are not references; a trailing period is.
const BARE_SESSION_ID_RE = /(?<![\w./:#=@-])[0-9a-f]{32}(?![\w/-]|\.\w)/g;
const EXACT_SESSION_ID_RE = /^[0-9a-f]{32}$/;

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
}

/** The session id a link title marks as a bare reference, or ``null``. */
export function parseSessionIdReferenceTitle(title: unknown): string | null {
  if (typeof title !== "string" || !title.startsWith(SESSION_ID_REFERENCE_TITLE_PREFIX)) {
    return null;
  }
  const id = title.slice(SESSION_ID_REFERENCE_TITLE_PREFIX.length);
  return EXACT_SESSION_ID_RE.test(id) ? id : null;
}

function referenceLink(id: string, child: MarkdownNode): MarkdownNode {
  // A fragment href survives link hardening untouched; the id rides in the title.
  return {
    type: "link",
    url: "#session-id",
    title: `${SESSION_ID_REFERENCE_TITLE_PREFIX}${id}`,
    children: [child],
  };
}

function splitText(value: string): MarkdownNode[] | null {
  const parts: MarkdownNode[] = [];
  let last = 0;
  for (const match of value.matchAll(BARE_SESSION_ID_RE)) {
    const start = match.index;
    if (start > last) parts.push({ type: "text", value: value.slice(last, start) });
    parts.push(referenceLink(match[0], { type: "text", value: match[0] }));
    last = start + match[0].length;
  }
  if (parts.length === 0) return null;
  if (last < value.length) parts.push({ type: "text", value: value.slice(last) });
  return parts;
}

function markChildren(node: MarkdownNode): void {
  if (!node.children) return;
  // Text already inside a link keeps that link's target.
  if (node.type === "link" || node.type === "linkReference") return;
  const next: MarkdownNode[] = [];
  for (const child of node.children) {
    if (child.type === "text" && child.value) {
      const parts = splitText(child.value);
      if (parts) {
        next.push(...parts);
        continue;
      }
    }
    if (child.type === "inlineCode" && child.value && EXACT_SESSION_ID_RE.test(child.value)) {
      next.push(referenceLink(child.value, child));
      continue;
    }
    markChildren(child);
    next.push(child);
  }
  node.children = next;
}

// Remark plugin marking bare session ids in prose and inline code as reference links.
// Fenced code blocks, link text and ids embedded in longer tokens are left alone.
export function remarkSessionIdReferences() {
  return (tree: MarkdownNode) => markChildren(tree);
}
