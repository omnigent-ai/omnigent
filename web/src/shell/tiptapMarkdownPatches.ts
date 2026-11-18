// Runtime patches for @tiptap/markdown: text escaping, loose inline runs, soft breaks.
//
// ⚠️ This overrides a method on the exported MarkdownManager prototype.
// Upstream's `encodeTextForMarkdown` HTML-entity-escapes every `&`, `<`, and
// `>` in non-code text, so a file containing "Choose & switch" is rewritten
// to "Choose &amp; switch" on the first save after any edit. GitHub renders
// the two identically, but the byte churn pollutes diffs in every markdown
// file the editor touches.
//
// CommonMark only requires escaping when the character would change meaning
// on re-parse:
//   - `&` when it would form an entity reference (`&amp;`, `&#38;`, …)
//   - `<` when it could open an HTML tag / autolink (`<a`, `</`, `<!`, `<?`)
//   - `>` when it starts a line (would become a blockquote)
// Everything else round-trips verbatim.
//
// The method (and the `codeTypes` registry it reads) are `private` in the
// upstream .d.ts, hence the casts. The patch is pinned by the round-trip
// tests in TipTapHtmlPassthrough.test.ts — if a @tiptap/markdown upgrade
// changes the serialiser so this no longer composes, those tests fail loudly.

import { MarkdownManager } from "@tiptap/markdown";
import { marksEqual, type Editor, type JSONContent } from "@tiptap/core";
import type { Node as ProseMirrorNode, Schema } from "@tiptap/pm/model";

import {
  definedFootnoteLabels,
  documentFootnoteLabels,
  escapeFootnoteReferences,
  installFootnoteReferenceTokenizer,
  prepareFootnoteReferences,
  withFootnoteLabels,
} from "./TipTapFootnotes";
import { toBlockContent } from "./TipTapGitHubAlert";
import {
  documentLinkLabels,
  escapeLinkReferences,
  installLinkDefinitionTokenizer,
  prepareReferenceLinks,
  withLinkLabels,
} from "./TipTapLinkReferences";
import { SOFT_BREAK_NODE } from "./TipTapSoftBreak";
import { installIterativeSetextHeadings } from "./markdownSetextHeading";
import {
  recordParse,
  sourceBlockPlan,
  trackTopLevelTokens,
  withoutRecording,
} from "./markdownSourceBlocks";

/** Ampersands that would parse as an entity reference on the way back in. */
const ENTITY_AMP = /&(?=[a-zA-Z][a-zA-Z0-9]{1,31};|#\d{1,7};|#[xX][0-9a-fA-F]{1,6};)/g;
/** `<` that could open an HTML tag, closing tag, comment, or declaration. */
const TAG_OPEN = /<(?=[a-zA-Z/!?])/g;
/** `>` at the start of a line — would re-parse as a blockquote marker. */
const LINE_START_QUOTE = /(^|\n)>/g;

/** The private surface of MarkdownManager the patch needs to touch. */
interface SerializerInternals {
  /** Extension names whose nodes/marks are code contexts (no escaping). */
  codeTypes: Set<string>;
  encodeTextForMarkdown: (
    text: string,
    node: JSONContent,
    parentNode: JSONContent | undefined,
  ) => string;
}

function textNode(text: string, marks: JSONContent["marks"]): JSONContent {
  return marks ? { type: "text", text, marks } : { type: "text", text };
}

/**
 * Inverse of `splitSoftBreaks`: turn every softBreak back into a "\n" text
 * node with the same marks and merge it into its same-mark neighbours, so the
 * serialiser sees exactly the document it would have seen without the split
 * (a mark spanning a soft break stays one `**…**` run). Text that was never
 * split apart is left alone.
 */
function joinSoftBreaks(node: JSONContent): JSONContent {
  if (node.content == null) return node;
  // Leading, trailing or repeated soft breaks (a wrapped line emptied by edits)
  // would serialise as blank lines that split the block on reload; keep only
  // single breaks between content.
  const children: JSONContent[] = [];
  for (const child of node.content) {
    const isBreak = child.type === SOFT_BREAK_NODE;
    if (isBreak && (children.length === 0 || children.at(-1)?.type === SOFT_BREAK_NODE)) continue;
    children.push(child);
  }
  while (children.at(-1)?.type === SOFT_BREAK_NODE) children.pop();
  const content: JSONContent[] = [];
  let afterBreak = false;
  for (const child of children) {
    const isBreak = child.type === SOFT_BREAK_NODE;
    const next = isBreak ? textNode("\n", child.marks) : joinSoftBreaks(child);
    const prev = content[content.length - 1];
    if (
      (isBreak || afterBreak) &&
      prev?.type === "text" &&
      next.type === "text" &&
      marksEqual(prev.marks ?? [], next.marks ?? [])
    ) {
      content[content.length - 1] = { ...prev, text: (prev.text ?? "") + (next.text ?? "") };
    } else {
      // Marks on a newline that found no same-marked neighbour carry no meaning
      // and would serialise as `**\n**`.
      content.push(isBreak ? textNode("\n", undefined) : next);
    }
    afterBreak = isBreak;
  }
  return { ...node, content };
}

/**
 * Run *callback* on *content* prepared for serialising, with the document's
 * defined footnote and link labels in scope.
 */
function withSerializeContext<T>(
  manager: MarkdownManager,
  content: JSONContent,
  callback: (prepared: JSONContent) => T,
): T {
  const joined = hasSoftBreakNode(manager) ? joinSoftBreaks(content) : content;
  // Literal `[^x]` or `[x]` text needs escaping only when the label is defined.
  return withFootnoteLabels(documentFootnoteLabels(joined), () =>
    withLinkLabels(documentLinkLabels(joined), () =>
      callback(prepareReferenceLinks(prepareFootnoteReferences(joined))),
    ),
  );
}

/** The private renderer `serialize` uses for each block. */
interface RendererInternals {
  renderNodeToMarkdown: (
    node: JSONContent,
    parentNode: JSONContent,
    index: number,
    level: number,
  ) => string;
}

/**
 * Serialise the top-level blocks at *indexes* as `serialize` writes them in *content*.
 *
 * :returns: Markdown by block index, or null if the renderer is unavailable.
 */
function serializeTopLevelBlocks(
  manager: MarkdownManager,
  content: JSONContent,
  indexes: number[],
): Map<number, string> | null {
  const { renderNodeToMarkdown } = manager as unknown as Partial<RendererInternals>;
  if (!renderNodeToMarkdown) return null;
  return withSerializeContext(manager, content, (prepared) => {
    const blocks = prepared.content ?? [];
    if (blocks.length !== (content.content?.length ?? 0)) return null;
    return new Map(
      indexes.map((index) => [
        index,
        renderNodeToMarkdown.call(manager, blocks[index], prepared, index, 0),
      ]),
    );
  });
}

/** The nesting of block nodes in *content*, ignoring inline content. */
function blockStructure(schema: Schema, content: JSONContent[]): string {
  return content
    .filter((node) => node.type != null && !schema.nodes[node.type]?.isInline)
    .map((node) => `${node.type}(${blockStructure(schema, node.content ?? [])})`)
    .join(",");
}

/** A piece of the saved document and the blocks it must read back as. */
interface SavedPiece {
  markdown: string;
  kept: boolean;
  head: { source: string; nodes: ProseMirrorNode[] };
  tail: { source: string; nodes: ProseMirrorNode[] };
}

/**
 * Whether two adjacent pieces read back as their own blocks. Kept source can
 * change meaning next to a new neighbour: indented code after a list joins the
 * list, and an unclosed fence takes in what follows.
 */
function readsBackSeparately(manager: MarkdownManager, left: SavedPiece, right: SavedPiece) {
  const { schema } = left.tail.nodes[0].type;
  const nodes = [...left.tail.nodes, ...right.head.nodes].map((node) => node.toJSON());
  const parsed = withoutRecording(() =>
    manager.parse(`${left.tail.source}\n\n${right.head.source}`),
  );
  return blockStructure(schema, parsed.content ?? []) === blockStructure(schema, nodes);
}

/**
 * The editor's Markdown, with top-level blocks the user did not change written
 * as their original source (see markdownSourceBlocks.ts).
 *
 * :param editor: The Markdown editor.
 * :returns: The document's Markdown; `editor.getMarkdown()` when no source is
 *     recorded or kept source would read back differently.
 */
export function markdownKeepingSource(editor: Editor): string {
  const plan = sourceBlockPlan(editor);
  const manager = editor.markdown;
  if (!plan || !manager) return editor.getMarkdown();
  const indexes = plan.filter((step): step is number => typeof step === "number");
  const rendered = indexes.length
    ? serializeTopLevelBlocks(manager, editor.getJSON(), indexes)
    : new Map<number, string>();
  if (!rendered) return editor.getMarkdown();
  const pieces: SavedPiece[] = plan.map((step) => {
    if (typeof step !== "number") return { markdown: step.source, kept: true, ...step };
    const markdown = rendered.get(step) ?? "";
    const edge = { source: markdown, nodes: [editor.state.doc.child(step)] };
    return { markdown, kept: false, head: edge, tail: edge };
  });
  // Serialised blocks always sat side by side; check each boundary kept source gained.
  const written = pieces.filter((piece) => piece.markdown.trim());
  for (let index = 1; index < written.length; index++) {
    const [left, right] = [written[index - 1], written[index]];
    if ((left.kept || right.kept) && !readsBackSeparately(manager, left, right))
      return editor.getMarkdown();
  }
  // `serialize` joins top-level blocks with a blank line.
  const markdown = pieces.map((piece) => piece.markdown).join("\n\n");
  return markdown.replace(/&nbsp;|\u00A0/g, "").trim() ? markdown : "";
}

let serializeWrapped = false;

/**
 * Install the minimal-escaping override. Idempotent — calling more than once
 * (e.g. from multiple editor modules) re-assigns the same implementation.
 * Also rejoins soft breaks (see TipTapSoftBreak.ts) before serialising.
 */
export function installMarkdownSerializerPatch(): void {
  if (!serializeWrapped) {
    serializeWrapped = true;
    const originalSerialize = MarkdownManager.prototype.serialize as (
      this: MarkdownManager,
      content: JSONContent,
      ...rest: unknown[]
    ) => string;
    MarkdownManager.prototype.serialize = function (
      this: MarkdownManager,
      content: JSONContent,
      ...rest: unknown[]
    ) {
      return withSerializeContext(this, content, (prepared) =>
        originalSerialize.call(this, prepared, ...rest),
      );
    };
  }
  const proto = MarkdownManager.prototype as unknown as SerializerInternals;
  proto.encodeTextForMarkdown = function (
    this: SerializerInternals,
    text: string,
    node: JSONContent,
    parentNode: JSONContent | undefined,
  ): string {
    // Same code-context check as upstream: literal characters are preserved
    // inside code marks / code blocks.
    const insideCode =
      (parentNode?.type != null && this.codeTypes.has(parentNode.type)) ||
      (node.marks ?? []).some((mark) =>
        this.codeTypes.has(typeof mark === "string" ? mark : (mark.type as string)),
      );
    if (insideCode) return text;
    const encoded = text
      .replace(ENTITY_AMP, "&amp;")
      .replace(TAG_OPEN, "&lt;")
      .replace(LINE_START_QUOTE, "$1\\>");
    // Link text never forms footnote references, so it needs no escaping.
    const insideLink = (node.marks ?? []).some(
      (mark) => (typeof mark === "string" ? mark : mark.type) === "link",
    );
    return insideLink ? encoded : escapeLinkReferences(escapeFootnoteReferences(encoded));
  };
}

// ---------------------------------------------------------------------------
// Parser patch — wrap loose inline runs so block+ parents stay schema-valid
// ---------------------------------------------------------------------------

/**
 * Node types whose content expression is block-level (`block+` or
 * `paragraph block*`) in the editor schema. A bare inline child under any of
 * these makes the parsed document schema-invalid.
 */
const BLOCK_CONTAINER_TYPES = new Set([
  "doc",
  "blockquote",
  "listItem",
  "taskItem",
  "tableCell",
  "tableHeader",
]);

/**
 * Recursively coerce every block container's children into valid block
 * content, wrapping runs of loose inline nodes in a paragraph (see
 * `toBlockContent`). Leaves inline parents (paragraph, heading, …) untouched.
 */
function wrapLooseInlineRuns(node: JSONContent): JSONContent {
  if (node.content == null) return node;
  const content = node.content.map(wrapLooseInlineRuns);
  return {
    ...node,
    content:
      node.type != null && BLOCK_CONTAINER_TYPES.has(node.type) ? toBlockContent(content) : content,
  };
}

/**
 * Replace each "\n" inside prose text with a softBreak node carrying the same
 * marks, so hard-wrapped source reflows to the editor width instead of
 * rendering one visual line per source line (see TipTapSoftBreak.ts). Nodes in
 * `verbatim` (the manager's `code: true` types) keep their literal newlines.
 */
function splitSoftBreaks(node: JSONContent, verbatim: ReadonlySet<string>): JSONContent {
  if (node.content == null || (node.type != null && verbatim.has(node.type))) return node;
  const content: JSONContent[] = [];
  for (const child of node.content) {
    if (child.type !== "text" || !child.text?.includes("\n")) {
      content.push(splitSoftBreaks(child, verbatim));
      continue;
    }
    child.text.split("\n").forEach((part, i) => {
      if (i > 0)
        content.push(
          child.marks ? { type: SOFT_BREAK_NODE, marks: child.marks } : { type: SOFT_BREAK_NODE },
        );
      if (part) content.push({ ...child, text: part });
    });
  }
  return { ...node, content };
}

/** The private surface of MarkdownManager the soft-break patches read. */
interface ParserInternals {
  /** Every extension registered with this manager (the editor's stack). */
  extensions: { name: string }[];
}

/** Whether this manager's editor registered the SoftBreak node; false if the
 *  private field disappears upstream, so parsing degrades to plain "\n" text. */
function hasSoftBreakNode(manager: MarkdownManager): boolean {
  const { extensions } = manager as unknown as Partial<ParserInternals>;
  return extensions?.some((ext) => ext.name === SOFT_BREAK_NODE) ?? false;
}

let parserPatchInstalled = false;

/**
 * Install a post-parse normalisation on `MarkdownManager.parse`. Idempotent.
 *
 * `@tiptap/markdown` (beta) can hand back a bare inline node directly under a
 * `block+` container — a standalone image in document flow (`\n\n![x](y)\n\n`,
 * or after `---`), or an image-first list item (`1. ![x](y)`). #2320 relaxed
 * `listItem` to `block+` for block-first children, but an INLINE first child
 * can't be fixed by any content expression on the parent — the parsed doc is
 * schema-invalid either way. ProseMirror loads it silently via `nodeFromJSON`
 * (no validation); the first transaction (a user edit, or StarterKit's
 * TrailingNode on load) calls `contentMatchAt` and throws ("Called
 * contentMatchAt on a node with invalid content"), crashing the whole file
 * panel. This is the "known residual" follow-up documented in #2320,
 * generalising #2004's blockquote-only `toBlockContent` guard to every block
 * container. The wrap is round-trip-safe: the serialiser emits a paragraph
 * holding a lone image as `![x](y)`, byte-identical to the source.
 *
 * When the editor registers the SoftBreak node, prose newlines are then split
 * into softBreak nodes; editors without it keep "\n" in their text.
 */
export function installMarkdownParserPatch(): void {
  if (parserPatchInstalled) return;
  parserPatchInstalled = true;
  const proto = MarkdownManager.prototype;
  const originalParse = proto.parse;
  proto.parse = function (this: MarkdownManager, markdown: string): JSONContent {
    installIterativeSetextHeadings(this.instance);
    installFootnoteReferenceTokenizer(this.instance);
    installLinkDefinitionTokenizer(this.instance);
    // `[^x]` is a footnote reference only when the file defines `[^x]:`.
    const { doc: parsed, groups } = trackTopLevelTokens(this, () =>
      withFootnoteLabels(definedFootnoteLabels(this.instance, markdown), () =>
        originalParse.call(this, markdown),
      ),
    );
    let doc = wrapLooseInlineRuns(parsed);
    // Without the manager's code-type registry no node can be proven verbatim,
    // so leave the newlines as text rather than risk splitting code content.
    const { codeTypes } = this as unknown as Partial<SerializerInternals>;
    if (hasSoftBreakNode(this) && codeTypes) doc = splitSoftBreaks(doc, codeTypes);
    recordParse(markdown, parsed, doc, groups);
    return doc;
  };
}
