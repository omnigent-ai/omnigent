// GFM footnotes for the markdown editor. Without these nodes marked reads
// `[^1]: url` as a link definition, and saves drop the definitions and inline
// every reference as `[^1](url)`. Both are kept verbatim and read-only here.

import { Extension, Node, type JSONContent } from "@tiptap/core";
import { NodeSelection, Plugin, TextSelection, type EditorState } from "@tiptap/pm/state";
import { interruptsParagraph } from "./markdownSetextHeading";

/** A footnote definition line, e.g. `[^1]: https://example.com`. */
const DEFINITION = /^ {0,3}\[\^[^\]\s]+\]:/;

/** Definition labels in a source, including inside quotes and lists, e.g. `1` in `> [^1]: url`. */
const DEFINITION_LABELS = /(?:^|[\s>])\[\^([^\]\s]+)\]:/g;

/** An indented line that continues a definition. */
const CONTINUATION = /^(?: {4}|\t)/;

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

/**
 * Collect the footnote labels defined in *markdown*.
 *
 * GitHub matches labels case-insensitively, so they are lower-cased.
 *
 * :param markdown: Markdown source, or the raw text of definition blocks.
 * :returns: The defined labels, e.g. `{"1", "note"}`.
 */
export function footnoteLabels(markdown: string): Set<string> {
  return new Set(
    Array.from(markdown.matchAll(DEFINITION_LABELS), (match) => match[1].toLowerCase()),
  );
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
      interruptsParagraph(`${line}\n`)
    ) {
      // A heading, list, fence, quote or table starts its own block.
      break;
    }
    end = next === -1 ? source.length : next;
  }

  let rawEnd = end;
  while (source[rawEnd] === "\n") rawEnd++;
  return source.slice(0, rawEnd);
}

/**
 * Drop marks from footnote references before serialising.
 *
 * Bolding a selection marks the references inside it too, and the serialiser
 * closes and reopens marks around a marked atom (`**a**[^1]** b**`), which
 * breaks the emphasis. Unmarked references keep it valid (`**a**[^1] **b**`).
 *
 * :param node: Document JSON about to be serialised.
 * :returns: *node* itself when nothing changed, else a copy with unmarked
 *     footnote references.
 */
export function stripFootnoteReferenceMarks(node: JSONContent): JSONContent {
  if (node.type === "footnoteReference") {
    return node.marks?.length ? { ...node, marks: undefined } : node;
  }
  if (node.content == null) return node;
  let changed = false;
  const content = node.content.map((child) => {
    const stripped = stripFootnoteReferenceMarks(child);
    changed ||= stripped !== child;
    return stripped;
  });
  return changed ? { ...node, content } : node;
}

/** Whether *state* has a footnote node selected as a whole. */
function footnoteSelected(state: EditorState): boolean {
  const { selection } = state;
  return selection instanceof NodeSelection && selection.node.type.name.startsWith("footnote");
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
        getAttrs: (element) => ({ raw: element.textContent ?? "" }),
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

  renderMarkdown: (node, _helpers, context) => {
    const raw = (node.attrs?.raw as string | undefined) ?? "";
    // List items join later blocks with one newline, which would make the
    // definition a lazy continuation of the item's paragraph.
    const nested = context?.parentType === "listItem" || context?.parentType === "taskItem";
    return nested && (context?.index ?? 0) > 0 ? `\n${raw}` : raw;
  },
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
        getAttrs: (element) => ({ label: element.textContent ?? "" }),
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

  markdownTokenizer: {
    name: "footnoteReference",
    level: "inline",
    start: (source) => source.indexOf("[^"),
    tokenize(source) {
      const match = REFERENCE.exec(source);
      if (!match || !isDefined(match[1])) return undefined;
      return { type: "footnoteReference", raw: match[0], label: match[1] };
    },
  },

  parseMarkdown: (token, helpers) =>
    helpers.createNode("footnoteReference", { label: String(token.label ?? "") }, []),

  renderMarkdown: (node) => `[^${(node.attrs?.label as string | undefined) ?? ""}]`,
});
