// Runtime patch for @tiptap/markdown's text escaping.
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
import type { JSONContent } from "@tiptap/core";

import { toBlockContent } from "./TipTapGitHubAlert";

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
  encodeTextForMarkdown(
    text: string,
    node: JSONContent,
    parentNode: JSONContent | undefined,
  ): string;
}

/**
 * Install the minimal-escaping override. Idempotent — calling more than once
 * (e.g. from multiple editor modules) re-assigns the same implementation.
 */
export function installMarkdownSerializerPatch(): void {
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
    return text
      .replace(ENTITY_AMP, "&amp;")
      .replace(TAG_OPEN, "&lt;")
      .replace(LINE_START_QUOTE, "$1\\>");
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
 */
export function installMarkdownParserPatch(): void {
  if (parserPatchInstalled) return;
  parserPatchInstalled = true;
  const proto = MarkdownManager.prototype;
  const originalParse = proto.parse;
  proto.parse = function (this: MarkdownManager, markdown: string): JSONContent {
    return wrapLooseInlineRuns(originalParse.call(this, markdown));
  };
}
