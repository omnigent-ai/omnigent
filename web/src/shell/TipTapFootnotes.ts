// GFM footnotes for the markdown editor. Without these nodes marked reads
// `[^1]: url` as a link definition, and saves drop the definitions and inline
// every reference as `[^1](url)`. Both are kept verbatim and read-only here.

import { Extension, Node, type JSONContent, type MarkdownToken } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";
import { NodeSelection, Plugin, TextSelection, type EditorState } from "@tiptap/pm/state";
import { interruptsParagraph } from "./markdownSetextHeading";
import { startsTable } from "./tiptapBoundedTokenizers";

/** A footnote definition line, e.g. `[^1]: https://example.com`. */
const DEFINITION = /^ {0,3}\[\^[^\]\s]+\]:/;

/** The label of each definition line in a definitions block, e.g. `1` in `[^1]: url`. */
const DEFINITION_LABELS = /^ {0,3}\[\^([^\]\s]+)\]:/gm;

/** Text that could hold a definition, possibly inside a quote or list. */
const DEFINITION_ANYWHERE = /\[\^[^\]\s]+\]:/;

/** An indented line that continues a definition. */
const CONTINUATION = /^(?: {4}|\t)/;

/** A thematic break or HTML block start (CommonMark types 1-6); both end a paragraph. */
const BREAK_OR_HTML_BLOCK = new RegExp(
  "^ {0,3}(?:([-*_])(?:[ \\t]*\\1){2,}[ \\t]*$|<(?:script|pre|style|textarea)(?:[\\s>]|$)|<!--|<\\?|<![A-Za-z]|<!\\[CDATA\\[|" +
    "</?(?:address|article|aside|base|basefont|blockquote|body|caption|center|col|colgroup|dd|details|dialog|dir|div|dl|dt|" +
    "fieldset|figcaption|figure|footer|form|frame|frameset|h[1-6]|head|header|hr|html|iframe|legend|li|link|main|menu|" +
    "menuitem|nav|noframes|ol|optgroup|option|p|param|search|section|summary|table|tbody|td|tfoot|th|thead|title|tr|" +
    "track|ul)(?:\\s|/?>|$))",
  "i",
);

/** A footnote label, e.g. `1` or `long-name`. */
const LABEL = /^[^\]\s]+$/;

/** A footnote reference; `[^1](url)` is an ordinary link and is left alone. */
const REFERENCE = /^\[\^([^\]\s]+)\](?!\()/;

/** Literal reference syntax in text, e.g. the `[` of `[^1]`. */
const LITERAL_REFERENCE = /\[(?=\^([^\]\s]+)\](?!\())/g;

/**
 * Labels defined by the document being parsed or serialised, or null when the
 * caller did not provide them (every `[^label]` is then treated as defined).
 * Parsing and serialising are synchronous, so a module slot is enough.
 */
let definedLabels: Set<string> | null = null;

/** Labels of the definition lines in *raw* definition blocks, lower-cased as GitHub matches them. */
function footnoteLabels(raw: string): Set<string> {
  return new Set(Array.from(raw.matchAll(DEFINITION_LABELS), (match) => match[1].toLowerCase()));
}

/**
 * Collect the footnote labels defined by a document's definition blocks.
 *
 * :param node: Document JSON.
 * :returns: The defined labels.
 */
export function documentFootnoteLabels(node: JSONContent): Set<string> {
  const raws: string[] = [];
  const visit = (current: JSONContent) => {
    if (current.type === "footnoteDefinitions") raws.push(String(current.attrs?.raw ?? ""));
    current.content?.forEach(visit);
  };
  visit(node);
  return footnoteLabels(raws.join("\n"));
}

/**
 * Collect the labels a markdown source defines, from its parsed definition blocks.
 *
 * Block-level parsing decides what is a definition, so `[^1]:` inside code or
 * mid-paragraph defines nothing, matching both the editor and GitHub.
 *
 * :param marked: The editor's configured marked instance.
 * :param markdown: Markdown source about to be parsed.
 * :returns: The defined labels, e.g. `{"1", "note"}`.
 */
export function definedFootnoteLabels(
  marked: MarkdownManager["instance"],
  markdown: string,
): Set<string> {
  if (!DEFINITION_ANYWHERE.test(markdown)) return new Set();
  // Definitions are block tokens, so the inline pass is skipped. Building a
  // Lexer re-targets marked's shared tokenizer; the real parse that follows
  // builds its own Lexer, which targets it back.
  const lexer = new marked.Lexer(marked.defaults);
  const tokens = withFootnoteLabels(new Set(), () =>
    lexer.blockTokens(markdown.replace(/\r\n?/g, "\n"), []),
  );
  const raws: string[] = [];
  const visit = (token: MarkdownToken) => {
    if (token.type === "footnoteDefinitions") raws.push(token.raw ?? "");
    for (const children of [token.tokens, token.items, token.nestedTokens]) {
      if (Array.isArray(children)) children.forEach(visit);
    }
  };
  tokens.forEach((token) => visit(token as MarkdownToken));
  return footnoteLabels(raws.join("\n"));
}

/**
 * Run *callback* with *labels* as the defined footnote labels.
 *
 * GitHub renders `[^x]` as a footnote only when `[^x]:` is defined, so parsing
 * leaves other `[^x]` text alone and serialising escapes only defined labels.
 *
 * :param labels: Labels defined by the document, see :func:`footnoteLabels`.
 * :param callback: The parse or serialise call.
 * :returns: The callback's result.
 */
export function withFootnoteLabels<T>(labels: Set<string>, callback: () => T): T {
  const previous = definedLabels;
  definedLabels = labels;
  try {
    return callback();
  } finally {
    definedLabels = previous;
  }
}

function isDefined(label: string): boolean {
  return definedLabels === null || definedLabels.has(label.toLowerCase());
}

/**
 * Escape literal text that would re-parse as a footnote reference.
 *
 * :param text: Text node content being serialised.
 * :returns: *text* with `[^label]` escaped as `\[^label]` for defined labels.
 */
export function escapeFootnoteReferences(text: string): string {
  return text.replace(LITERAL_REFERENCE, (bracket, label: string) =>
    isDefined(label) ? "\\[" : bracket,
  );
}

/**
 * Find the run of footnote definitions at the start of *source*.
 *
 * The run continues through lazy continuation lines until a line that would
 * start another block, and after a blank line through further definitions or
 * indented continuation paragraphs.
 *
 * :param source: Remaining markdown source at a block boundary.
 * :returns: The run including its trailing newlines, or null when *source*
 *     does not start with a definition.
 */
/** End of the line after the one ending at *end* (a newline index, or -1). */
function lineEndAfter(source: string, end: number): number {
  if (end === -1) return source.length;
  const next = source.indexOf("\n", end + 1);
  return next === -1 ? source.length : next;
}

export function scanFootnoteDefinitions(source: string): string | null {
  let end = source.indexOf("\n");
  if (end === -1) end = source.length;
  if (!DEFINITION.test(source.slice(0, end))) return null;

  while (end < source.length) {
    let start = end + 1;
    let next = source.indexOf("\n", start);
    let line = source.slice(start, next === -1 ? source.length : next);
    if (line.trim() === "") {
      // A blank line ends the run unless another definition or an indented
      // continuation follows it.
      while (line.trim() === "" && next !== -1) {
        start = next + 1;
        next = source.indexOf("\n", start);
        line = source.slice(start, next === -1 ? source.length : next);
      }
      if (!DEFINITION.test(line) && !CONTINUATION.test(line)) break;
    } else if (
      !DEFINITION.test(line) &&
      !CONTINUATION.test(line) &&
      (interruptsParagraph(`${line}\n`) ||
        BREAK_OR_HTML_BLOCK.test(line) ||
        startsTable(source.slice(start, lineEndAfter(source, next))))
    ) {
      // A line that would end a paragraph starts its own block.
      break;
    }
    end = next === -1 ? source.length : next;
  }

  let rawEnd = end;
  while (source[rawEnd] === "\n") rawEnd++;
  return source.slice(0, rawEnd);
}

/**
 * Prepare footnote references for serialising, so the saved text reads back the same.
 *
 * - Marks are dropped: the serialiser closes and reopens marks around a marked
 *   atom (`**a**[^1]** b**`), which breaks the emphasis.
 * - Text right after a reference is escaped when it would re-parse the
 *   reference: `(` as an inline link, `:` at the start of a block as a definition.
 *
 * :param node: Document JSON about to be serialised.
 * :returns: *node* itself when nothing changed, else a prepared copy.
 */
export function prepareFootnoteReferences(node: JSONContent): JSONContent {
  if (node.content == null) return node;
  let changed = false;
  const content = node.content.map((child, index, siblings) => {
    let prepared = child;
    if (child.type === "footnoteReference") {
      if (child.marks?.length) prepared = { ...child, marks: undefined };
    } else if (child.type === "text" && !child.marks?.length) {
      if (siblings[index - 1]?.type === "footnoteReference") {
        const text = child.text ?? "";
        // Only whitespace before the reference: it starts the block's first line.
        const opensBlock = siblings
          .slice(0, index - 1)
          .every((sibling) => sibling.type === "text" && !sibling.text?.trim());
        if (text.startsWith("(") || (opensBlock && text.startsWith(":"))) {
          prepared = { ...child, text: `\\${text}` };
        }
      }
    } else {
      prepared = prepareFootnoteReferences(child);
    }
    changed ||= prepared !== child;
    return prepared;
  });
  return changed ? { ...node, content } : node;
}

const installedReferenceTokenizers = new WeakSet<MarkdownManager["instance"]>();

/**
 * Register the `[^label]` tokenizer with *marked*.
 *
 * It is registered with marked directly, not through @tiptap/markdown, to see
 * whether it is inside link text: TipTap marks only text, so a reference there
 * would lose the link. Inside a link `[^1]` stays text, and the link round-trips.
 *
 * :param marked: The editor's configured marked instance.
 */
export function installFootnoteReferenceTokenizer(marked: MarkdownManager["instance"]): void {
  if (installedReferenceTokenizers.has(marked)) return;
  installedReferenceTokenizers.add(marked);
  marked.use({
    extensions: [
      {
        name: "footnoteReference",
        level: "inline",
        start: (source: string) => source.indexOf("[^"),
        tokenizer(source: string) {
          if (this.lexer.state.inLink) return undefined;
          const match = REFERENCE.exec(source);
          if (!match || !isDefined(match[1])) return undefined;
          return { type: "footnoteReference", raw: match[0], label: match[1] };
        },
      },
    ],
  });
}

/** Whether *state* has a footnote node selected as a whole. */
/** Nodes that hold Markdown source verbatim and are only edited in Source mode. */
const SOURCE_NODES = new Set(["footnoteReference", "footnoteDefinitions", "linkDefinitions"]);

/** Whether *state* has a verbatim source node selected as a whole. */
function footnoteSelected(state: EditorState): boolean {
  const { selection } = state;
  return selection instanceof NodeSelection && SOURCE_NODES.has(selection.node.type.name);
}

/**
 * Serialise a verbatim source block.
 *
 * :param raw: The block's source, trailing newlines trimmed.
 * :param context: The serialiser's render context.
 * :returns: The source to write.
 */
export function renderSourceBlock(
  raw: string,
  context: { parentType?: string | null; index?: number } | undefined,
): string {
  // List items join later blocks with one newline, which would make the
  // block a lazy continuation of the item's paragraph.
  const nested = context?.parentType === "listItem" || context?.parentType === "taskItem";
  return nested && (context?.index ?? 0) > 0 ? `\n${raw}` : raw;
}

/**
 * Keeps edits aimed at a selected footnote from replacing it. Typing, pasting
 * or Enter over a selected node would otherwise delete it silently; Backspace
 * and Delete still remove it.
 */
export const FootnoteProtection = Extension.create({
  name: "footnoteProtection",

  addProseMirrorPlugins() {
    return [
      new Plugin({
        props: {
          handleTextInput: (view) => footnoteSelected(view.state),
          handlePaste: (view) => footnoteSelected(view.state),
          handleKeyDown: (view, event) => {
            const { selection } = view.state;
            if (
              event.key === "Enter" &&
              selection instanceof NodeSelection &&
              selection.node.type.name === "footnoteReference"
            ) {
              // Split after the reference instead of replacing it.
              view.dispatch(
                view.state.tr.setSelection(TextSelection.create(view.state.doc, selection.to)),
              );
            }
            return false;
          },
        },
      }),
    ];
  },
});

export const FootnoteDefinitions = Node.create({
  name: "footnoteDefinitions",
  group: "block",
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      // The exact source of the definition run, trailing newlines trimmed (the
      // serialiser re-joins blocks with blank lines).
      raw: { default: "", rendered: false },
    };
  },

  // Copy and paste carry the node through its rendered HTML.
  parseHTML() {
    return [
      {
        tag: "div[data-footnote-definitions]",
        // Reject pasted text that would not read back as a definition run.
        getAttrs: (element) => {
          const raw = element.textContent ?? "";
          return scanFootnoteDefinitions(raw)?.replace(/\n+$/, "") === raw ? { raw } : false;
        },
      },
    ];
  },

  renderHTML({ node }) {
    return [
      "div",
      { "data-footnote-definitions": "", class: "md-footnote-definitions" },
      (node.attrs.raw as string | undefined) ?? "",
    ];
  },

  markdownTokenizer: {
    name: "footnoteDefinitions",
    level: "block",
    // Definitions never interrupt a paragraph. An explicit start also keeps
    // @tiptap/markdown from probing with a throwaway lexer, which re-targets
    // marked's shared tokenizer and loses the paragraphs' inline tokens.
    start: () => -1,
    tokenize(source) {
      const raw = scanFootnoteDefinitions(source);
      return raw === null ? undefined : { type: "footnoteDefinitions", raw };
    },
  },

  parseMarkdown: (token, helpers) =>
    helpers.createNode("footnoteDefinitions", { raw: (token.raw ?? "").replace(/\n+$/, "") }, []),

  renderMarkdown: (node, _helpers, context) =>
    renderSourceBlock((node.attrs?.raw as string | undefined) ?? "", context),
});

export const FootnoteReference = Node.create({
  name: "footnoteReference",
  group: "inline",
  inline: true,
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      label: { default: "", rendered: false },
    };
  },

  parseHTML() {
    return [
      {
        tag: "sup[data-footnote-reference]",
        getAttrs: (element) => {
          const label = element.textContent ?? "";
          return LABEL.test(label) ? { label } : false;
        },
      },
    ];
  },

  renderHTML({ node }) {
    return [
      "sup",
      { "data-footnote-reference": "", class: "md-footnote-reference" },
      (node.attrs.label as string | undefined) ?? "",
    ];
  },

  parseMarkdown: (token, helpers) =>
    helpers.createNode("footnoteReference", { label: String(token.label ?? "") }, []),

  renderMarkdown: (node) => `[^${(node.attrs?.label as string | undefined) ?? ""}]`,
});
