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

/** Whether a reference to `id` that ends `block` may be the start of a longer token. */
export function sessionIdMayStillGrow(
  markdown: string,
  block: string,
  id: string,
  streaming: boolean,
): boolean {
  if (streaming && markdown.replace(TRAILING_CLOSING_PUNCTUATION_RE, "").endsWith(id)) return true;
  // A block rendered from older text is stale once that text runs on in the message.
  const blockText = block.trimEnd();
  if (!blockText) return false;
  let found = false;
  for (let at = markdown.indexOf(blockText); at !== -1; at = markdown.indexOf(blockText, at + 1)) {
    if (!/\S/.test(markdown.charAt(at + blockText.length))) return false;
    found = true;
  }
  return found;
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

// Formatting whose text reads as part of the surrounding words.
const FORMATTING_TYPES = new Set(["emphasis", "strong", "delete"]);
// Inline nodes that join the surrounding word but whose own text is never a reference.
const OPAQUE_INLINE_TYPES = new Set([
  "link",
  "linkReference",
  "html",
  "image",
  "imageReference",
  "inlineMath",
  "footnoteReference",
]);
const OPAQUE_CHARACTER = "\u0000";
const OPENING_ANCHOR_RE = /^<a[\s>]/i;
const CLOSING_ANCHOR_RE = /^<\/a\s*>/i;
const SELF_CONTAINED_ANCHOR_RE = /<\/a\s*>|\/>\s*$/i;

interface InlineLeaf {
  node: MarkdownNode;
  parent: MarkdownNode;
  start: number;
  end: number;
  insideAnchor: boolean;
}

// The text a block's inline content reads as, with the text and code leaves it came from.
interface InlineRun {
  text: string;
  leaves: InlineLeaf[];
}

function inlineRuns(tree: MarkdownNode): InlineRun[] {
  const runs: InlineRun[] = [];
  let run: InlineRun = { text: "", leaves: [] };
  let anchorDepth = 0;
  const flush = () => {
    if (run.leaves.length) runs.push(run);
    run = { text: "", leaves: [] };
    anchorDepth = 0;
  };
  const visit = (parent: MarkdownNode) => {
    for (const child of parent.children ?? []) {
      // An authored link cannot pose as a reference by copying the marker title.
      if (parseSessionIdReferenceTitle(child.title)) child.title = null;
      if (child.type === "text" || child.type === "inlineCode") {
        const value = child.value ?? "";
        const start = run.text.length;
        run.leaves.push({
          node: child,
          parent,
          start,
          end: start + value.length,
          insideAnchor: anchorDepth > 0,
        });
        run.text += value;
      } else if (child.type === "break") {
        run.text += "\n";
      } else if (FORMATTING_TYPES.has(child.type)) {
        visit(child);
      } else if (OPAQUE_INLINE_TYPES.has(child.type)) {
        // Raw HTML anchors are separate open and close nodes around their text.
        const html = child.type === "html" ? (child.value ?? "") : "";
        if (OPENING_ANCHOR_RE.test(html) && !SELF_CONTAINED_ANCHOR_RE.test(html)) anchorDepth += 1;
        else if (CLOSING_ANCHOR_RE.test(html)) anchorDepth = Math.max(0, anchorDepth - 1);
        run.text += OPAQUE_CHARACTER;
      } else {
        flush();
        visit(child);
        flush();
      }
    }
  };
  visit(tree);
  flush();
  return runs;
}

interface ReferenceSpan {
  id: string;
  start: number;
  trailing: boolean;
}

function replacementNodes(leaf: InlineLeaf, spans: ReferenceSpan[]): MarkdownNode[] {
  if (leaf.node.type === "inlineCode") {
    return [referenceLink(spans[0].id, leaf.node, spans[0].trailing)];
  }
  const value = leaf.node.value ?? "";
  const nodes: MarkdownNode[] = [];
  let offset = 0;
  for (const span of spans) {
    const start = span.start - leaf.start;
    if (start > offset) nodes.push({ type: "text", value: value.slice(offset, start) });
    nodes.push(referenceLink(span.id, { type: "text", value: span.id }, span.trailing));
    offset = start + span.id.length;
  }
  if (offset < value.length) nodes.push({ type: "text", value: value.slice(offset) });
  return nodes;
}

function markSessionIds(tree: MarkdownNode): void {
  const runs = inlineRuns(tree);
  const spansByLeaf = new Map<InlineLeaf, ReferenceSpan[]>();
  runs.forEach((run, runIndex) => {
    const textEnd = run.text.trimEnd().length;
    for (const word of run.text.matchAll(/\S+/g)) {
      const match = SESSION_ID_WORD_RE.exec(word[0]);
      if (!match) continue;
      const [, opening, id] = match;
      const start = word.index + opening.length;
      const end = start + id.length;
      const leaf = run.leaves.find((candidate) => candidate.start <= start && end <= candidate.end);
      if (!leaf || leaf.insideAnchor) continue;
      if (leaf.node.type === "inlineCode" && (leaf.start !== start || leaf.end !== end)) continue;
      const trailing = runIndex === runs.length - 1 && word.index + word[0].length === textEnd;
      spansByLeaf.set(leaf, [...(spansByLeaf.get(leaf) ?? []), { id, start, trailing }]);
    }
  });
  const replacements = new Map<MarkdownNode, MarkdownNode[]>();
  const parents = new Set<MarkdownNode>();
  for (const [leaf, spans] of spansByLeaf) {
    replacements.set(leaf.node, replacementNodes(leaf, spans));
    parents.add(leaf.parent);
  }
  for (const parent of parents) {
    parent.children = parent.children?.flatMap((child) => replacements.get(child) ?? [child]);
  }
}

// Remark plugin marking bare session ids in prose and inline code as reference links.
// Fenced code blocks, link text and ids embedded in longer tokens are left alone.
export function remarkSessionIdReferences() {
  return markSessionIds;
}
