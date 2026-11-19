// A markdown link whose title starts with this names a bare session id the
// message mentioned; the renderer turns it into a session pill once that
// session resolves, and shows the text as written otherwise.
const SESSION_ID_REFERENCE_TITLE_PREFIX = "omnigent-session-id:";
export const SESSION_ID_REFERENCE_HREF = "#session-id";

// A whitespace-delimited word that is the server's session id form (32 lowercase
// hex chars), optionally wrapped in brackets, quotes or emphasis or followed by punctuation.
// Paths, URLs, query values, file names and longer tokens are not references.
const CLOSING_PUNCTUATION = `)\\]}"'”’*_~.,;:!?`;
const SESSION_ID_WORD_RE = new RegExp(`^([([{"'“‘*_~]*)([0-9a-f]{32})([${CLOSING_PUNCTUATION}]*)$`);
const EXACT_SESSION_ID_RE = /^[0-9a-f]{32}$/;
const TRAILING_CLOSING_PUNCTUATION_RE = new RegExp(`[${CLOSING_PUNCTUATION}]+$`);

// Marks the id that ends a parsed block, which may still be growing while text streams in.
const TRAILING_SUFFIX = ":end";

/** Whether streamed text ends in `id`, so more characters could still extend that word. */
export function streamedTextEndsWithId(markdown: string, id: string): boolean {
  return markdown.replace(TRAILING_CLOSING_PUNCTUATION_RE, "").endsWith(id);
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  title?: string | null;
  children?: MarkdownNode[];
}

export interface SessionIdReferenceTarget {
  id: string;
  trailing: boolean;
}

/** The session id a link title marks as a bare reference, or ``null``. */
export function parseSessionIdReferenceTitle(title: unknown): SessionIdReferenceTarget | null {
  if (typeof title !== "string" || !title.startsWith(SESSION_ID_REFERENCE_TITLE_PREFIX)) {
    return null;
  }
  const rest = title.slice(SESSION_ID_REFERENCE_TITLE_PREFIX.length);
  const trailing = rest.endsWith(TRAILING_SUFFIX);
  const id = trailing ? rest.slice(0, -TRAILING_SUFFIX.length) : rest;
  return EXACT_SESSION_ID_RE.test(id) ? { id, trailing } : null;
}

function referenceLink(id: string, child: MarkdownNode, trailing = false): MarkdownNode {
  // A fragment href survives link hardening untouched; the id rides in the title.
  return {
    type: "link",
    url: SESSION_ID_REFERENCE_HREF,
    title: `${SESSION_ID_REFERENCE_TITLE_PREFIX}${id}${trailing ? TRAILING_SUFFIX : ""}`,
    children: [child],
  };
}

function splitText(value: string, endsBlock: boolean): MarkdownNode[] | null {
  // Odd indexes hold the whitespace between words.
  const pieces = value.split(/(\s+)/);
  const lastWordIndex = endsBlock ? pieces.length - 1 : -1;
  const parts: MarkdownNode[] = [];
  let pending = "";
  let found = false;
  pieces.forEach((piece, index) => {
    const match = index % 2 === 0 && SESSION_ID_WORD_RE.exec(piece);
    if (!match) {
      pending += piece;
      return;
    }
    const [, opening, id, closing] = match;
    pending += opening;
    if (pending) parts.push({ type: "text", value: pending });
    parts.push(referenceLink(id, { type: "text", value: id }, index === lastWordIndex));
    pending = closing;
    found = true;
  });
  if (!found) return null;
  if (pending) parts.push({ type: "text", value: pending });
  return parts;
}

function lastLeaf(node: MarkdownNode): MarkdownNode {
  let leaf = node;
  while (leaf.children?.length) leaf = leaf.children[leaf.children.length - 1];
  return leaf;
}

function markChildren(node: MarkdownNode, finalLeaf: MarkdownNode): void {
  if (!node.children) return;
  // Text already inside a link keeps that link's target.
  if (node.type === "link" || node.type === "linkReference") return;
  const next: MarkdownNode[] = [];
  for (const child of node.children) {
    const endsBlock = child === finalLeaf;
    if (child.type === "text" && child.value) {
      const parts = splitText(child.value, endsBlock);
      if (parts) {
        next.push(...parts);
        continue;
      }
    }
    if (child.type === "inlineCode" && child.value && EXACT_SESSION_ID_RE.test(child.value)) {
      next.push(referenceLink(child.value, child, endsBlock));
      continue;
    }
    markChildren(child, finalLeaf);
    next.push(child);
  }
  node.children = next;
}

// Remark plugin marking bare session ids in prose and inline code as reference links.
// Fenced code blocks, link text and ids embedded in longer tokens are left alone.
export function remarkSessionIdReferences() {
  return (tree: MarkdownNode) => markChildren(tree, lastLeaf(tree));
}
