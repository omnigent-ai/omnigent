// Reference-style links for the markdown editor. Saves used to inline every
// `[text][ref]` and drop the `[ref]: url` lines; links keep their reference
// form and definitions are kept verbatim, shown read-only.

import { Node, type JSONContent, type MarkdownToken } from "@tiptap/core";
import type { MarkdownManager } from "@tiptap/markdown";
import { renderSourceBlock } from "./TipTapFootnotes";

/** A link reference definition line, e.g. `[docs]: https://example.com`; not a footnote. */
const DEFINITION_LINE = /^ {0,3}\[(?!\^)(?:\\.|[^\\\]])+\]:/;

/** The label of each definition line in a definitions block. */
const DEFINITION_LABELS = /^ {0,3}\[(?!\^)((?:\\.|[^\\\]])+)\]:/gm;

/** `[text][label]`, capturing the label. */
const FULL_REFERENCE = /^\[[\s\S]*\]\[((?:\\.|[^\\[\]])+)\]$/;

/** Literal `[label]` text, capturing the label. */
const LITERAL_LABEL = /\[(?=((?:\\.|[^\\[\]\n])+)\])/g;

/**
 * Labels defined by the document being serialised, or null when the caller did
 * not provide them. Serialising is synchronous, so a module slot is enough.
 */
let definedLabels: Set<string> | null = null;

/** Normalise a label the way marked matches it: case-insensitive, whitespace collapsed. */
export function normalizeLinkLabel(label: string): string {
  return label.toLowerCase().replace(/\s+/g, " ");
}

/**
 * Collect the link labels defined by a document's definition blocks.
 *
 * :param node: Document JSON.
 * :returns: The normalised labels, e.g. `{"docs"}`.
 */
export function documentLinkLabels(node: JSONContent): Set<string> {
  const labels = new Set<string>();
  const visit = (current: JSONContent) => {
    if (current.type === "linkDefinitions") {
      for (const match of String(current.attrs?.raw ?? "").matchAll(DEFINITION_LABELS)) {
        labels.add(normalizeLinkLabel(match[1]));
      }
    }
    current.content?.forEach(visit);
  };
  visit(node);
  return labels;
}

/**
 * Run *callback* with *labels* as the document's defined link labels.
 *
 * :param labels: Labels from :func:`documentLinkLabels`.
 * :param callback: The serialise call.
 * :returns: The callback's result.
 */
export function withLinkLabels<T>(labels: Set<string>, callback: () => T): T {
  const previous = definedLabels;
  definedLabels = labels;
  try {
    return callback();
  } finally {
    definedLabels = previous;
  }
}

/**
 * Escape literal text that would re-parse as a reference link.
 *
 * :param text: Text node content being serialised, outside link text.
 * :returns: *text* with `[label]` escaped as `\[label]` for defined labels.
 */
export function escapeLinkReferences(text: string): string {
  const labels = definedLabels;
  if (!labels?.size) return text;
  return text.replace(LITERAL_LABEL, (bracket, label: string) =>
    labels.has(normalizeLinkLabel(label)) ? "\\[" : bracket,
  );
}

/**
 * Describe a parsed link's reference form, so a save can write it back.
 *
 * :param token: The marked `link` token.
 * :returns: Link mark attributes; all null for an inline link or autolink.
 */
export function referenceAttributes(token: MarkdownToken): Record<string, string | null> {
  const raw = String(token.raw ?? "");
  const none = { reference: null, referenceLabel: null, referenceHref: null, referenceTitle: null };
  // An inline link ends with `)`; autolinks and bare URLs don't open with `[`.
  if (!raw.startsWith("[") || raw.endsWith(")")) return none;
  let reference: string;
  let label: string;
  const full = FULL_REFERENCE.exec(raw);
  if (raw.endsWith("][]")) {
    reference = "[]";
    label = raw.slice(1, -3);
  } else if (full) {
    reference = `[${full[1]}]`;
    label = full[1];
  } else {
    reference = "";
    label = raw.slice(1, -1);
  }
  return {
    reference,
    referenceLabel: normalizeLinkLabel(label),
    referenceHref: String(token.href ?? ""),
    referenceTitle: token.title ? String(token.title) : null,
  };
}

/**
 * Write a link in its original reference form when that still means the same link.
 *
 * TipTap renders a mark around a placeholder for its text, so the text check
 * for `[text][]` and `[text]` happens earlier, in :func:`prepareReferenceLinks`.
 *
 * :param attrs: The link mark's attributes.
 * :returns: The reference suffix after `[text]` (`[label]`, `[]` or ``), or null
 *     to write the link inline.
 */
export function referenceSuffix(attrs: Record<string, unknown>): string | null {
  const reference = attrs.reference;
  if (typeof reference !== "string") return null;
  // An edited URL or title, or a removed definition, needs an inline link.
  if (attrs.href !== attrs.referenceHref) return null;
  if ((attrs.title ?? null) !== (attrs.referenceTitle ?? null)) return null;
  const label = attrs.referenceLabel;
  if (typeof label !== "string" || (definedLabels && !definedLabels.has(label))) return null;
  return reference;
}

type Mark = NonNullable<JSONContent["marks"]>[number];

function linkMark(node: JSONContent): Mark | undefined {
  return node.marks?.find((mark) => mark.type === "link");
}

/**
 * Drop the reference form of `[text][]` and `[text]` links whose text no longer
 * matches their label, before serialising; those links are written inline.
 *
 * :param node: Document JSON about to be serialised.
 * :returns: *node* itself when nothing changed, else a prepared copy.
 */
export function prepareReferenceLinks(node: JSONContent): JSONContent {
  if (node.content == null) return node;
  let changed = false;
  const content = node.content.map((child) => {
    const prepared = prepareReferenceLinks(child);
    changed ||= prepared !== child;
    return prepared;
  });
  // Group adjacent children that share one link, then compare its text.
  for (let start = 0; start < content.length;) {
    const mark = linkMark(content[start]);
    let end = start + 1;
    while (end < content.length && JSON.stringify(linkMark(content[end])) === JSON.stringify(mark))
      end++;
    const reference = mark?.attrs?.reference;
    if (mark && (reference === "" || reference === "[]")) {
      const text = content
        .slice(start, end)
        .map((child) => child.text ?? "")
        .join("");
      if (normalizeLinkLabel(text) !== mark.attrs?.referenceLabel) {
        const inline = { ...mark, attrs: { ...mark.attrs, reference: null } };
        for (let index = start; index < end; index++) {
          const marks = content[index].marks?.map((other) =>
            other === mark || other.type === "link" ? inline : other,
          );
          content[index] = { ...content[index], marks };
        }
        changed = true;
      }
    }
    start = end;
  }
  return changed ? { ...node, content } : node;
}

/** The parts of marked's lexer the definition tokenizer uses. */
interface DefinitionLexer {
  tokens: { links: Record<string, { href: string; title?: string }> };
  tokenizer: {
    def: (source: string) => { tag: string; raw: string; href: string; title?: string } | undefined;
  };
}

const installedDefinitionTokenizers = new WeakSet<MarkdownManager["instance"]>();

/**
 * Register a tokenizer that keeps runs of link reference definitions as one block.
 *
 * It uses marked's own definition tokenizer and records each definition, as
 * marked does, so reference links still resolve. Footnote definitions are left
 * to the footnote tokenizer.
 *
 * :param marked: The editor's configured marked instance.
 */
export function installLinkDefinitionTokenizer(marked: MarkdownManager["instance"]): void {
  if (installedDefinitionTokenizers.has(marked)) return;
  installedDefinitionTokenizers.add(marked);
  marked.use({
    extensions: [
      {
        name: "linkDefinitions",
        level: "block",
        tokenizer(source: string, tokens: { type: string }[]) {
          // marked folds a definition right after a paragraph into its text.
          const previous = tokens.at(-1)?.type;
          if (previous === "paragraph" || previous === "text") return undefined;
          // marked keeps its block tokenizers private; `def` is its own parser.
          const lexer = this.lexer as unknown as DefinitionLexer;
          const { links } = lexer.tokens;
          let raw = "";
          for (;;) {
            const definition = lexer.tokenizer.def(source.slice(raw.length));
            if (!definition || definition.tag.startsWith("^")) break;
            links[definition.tag] ??= { href: definition.href, title: definition.title };
            raw += definition.raw;
          }
          return raw ? { type: "linkDefinitions", raw } : undefined;
        },
      },
    ],
  });
}

/** Whether *raw* reads back as a run of link definitions. */
function isDefinitionRun(raw: string): boolean {
  const lines = raw.split("\n");
  return (
    DEFINITION_LINE.test(lines[0]) &&
    lines.every((line) => !line.trim() || DEFINITION_LINE.test(line) || /^\s/.test(line))
  );
}

export const LinkDefinitions = Node.create({
  name: "linkDefinitions",
  group: "block",
  atom: true,
  selectable: true,

  addAttributes() {
    return {
      // The exact source of the definition run, trailing newlines trimmed.
      raw: { default: "", rendered: false },
    };
  },

  // Copy and paste carry the node through its rendered HTML.
  parseHTML() {
    return [
      {
        tag: "div[data-link-definitions]",
        getAttrs: (element) => {
          const raw = element.textContent ?? "";
          return isDefinitionRun(raw) ? { raw } : false;
        },
      },
    ];
  },

  renderHTML({ node }) {
    return [
      "div",
      { "data-link-definitions": "", class: "md-link-definitions" },
      (node.attrs.raw as string | undefined) ?? "",
    ];
  },

  markdownTokenName: "linkDefinitions",

  parseMarkdown: (token, helpers) =>
    helpers.createNode("linkDefinitions", { raw: (token.raw ?? "").replace(/\n+$/, "") }, []),

  renderMarkdown: (node, _helpers, context) =>
    renderSourceBlock((node.attrs?.raw as string | undefined) ?? "", context),
});
