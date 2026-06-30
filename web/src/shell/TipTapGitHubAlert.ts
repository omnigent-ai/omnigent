// GitHub-alert-aware Blockquote for the markdown editor.
//
// GitHub renders blockquotes whose first line is a `[!NOTE]` / `[!TIP]` /
// `[!IMPORTANT]` / `[!WARNING]` / `[!CAUTION]` marker as colored callouts
// with an icon and label, hiding the marker itself. This extension does the
// same: the marker is detected at markdown parse time, stored as an
// `alertType` node attr (rendered as `data-alert-type` / `data-alert-label`
// for the CSS in index.css), and stripped from the visible content. The
// serializer re-emits the marker line so files round-trip unchanged.
//
// Use INSTEAD of the stock Blockquote, with
// ``StarterKit.configure({ blockquote: false })`` — StarterKit bundles its
// own Blockquote and a duplicate would win the markdown token registration.

import { Blockquote } from "@tiptap/extension-blockquote";
import type { JSONContent } from "@tiptap/core";

/** GitHub alert types, lowercase as stored in the `alertType` attr. */
const ALERT_TYPES = ["note", "tip", "important", "warning", "caution"] as const;
type AlertType = (typeof ALERT_TYPES)[number];

/** Display labels, matching GitHub's callout headers. */
const ALERT_LABELS: Record<AlertType, string> = {
  note: "Note",
  tip: "Tip",
  important: "Important",
  warning: "Warning",
  caution: "Caution",
};

/** Matches a marker at the start of the quote's first paragraph text,
 *  e.g. "[!NOTE]\nrest of the paragraph…" or a bare "[!NOTE]". */
const ALERT_MARKER = /^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\](\n|$)/;

/** Result of stripping an alert marker from parsed blockquote children. */
interface ExtractedAlert {
  /** The detected alert type, or null when the quote is a plain blockquote. */
  alertType: AlertType | null;
  /** Children with the marker text removed. */
  children: JSONContent[];
}

/**
 * Detect and strip a GitHub alert marker from parsed blockquote children.
 *
 * Handles both source shapes: the marker sharing the first paragraph with
 * the body (``> [!NOTE]\n> text`` — strip the prefix from the text node)
 * and a standalone marker paragraph (``> [!NOTE]\n>\n> text`` — drop the
 * paragraph). Per the GitHub spec the marker must open the quote; anything
 * else is left untouched.
 *
 * :param children: Parsed block children of the blockquote token.
 * :returns: The alert type (or null) and the children without the marker.
 */
export function extractAlert(children: JSONContent[]): ExtractedAlert {
  const first = children[0];
  const firstText = first?.type === "paragraph" ? first.content?.[0] : undefined;
  if (firstText?.type !== "text" || typeof firstText.text !== "string") {
    return { alertType: null, children };
  }
  const match = ALERT_MARKER.exec(firstText.text);
  if (!match) {
    return { alertType: null, children };
  }
  const alertType = match[1].toLowerCase() as AlertType;
  const stripped = firstText.text.slice(match[0].length);
  // `first.content!` below is safe: firstText (first.content[0]) was just
  // verified to be a text node, so first is a paragraph with content.
  if (stripped === "" && first.content!.length === 1) {
    // Marker-only paragraph: drop it (keep one empty paragraph if it was
    // the only child — blockquote content requires at least one block).
    const rest = children.slice(1);
    return { alertType, children: rest.length > 0 ? rest : [{ type: "paragraph" }] };
  }
  const restoredFirst: JSONContent = {
    ...first,
    content: [
      // Empty text nodes are invalid in ProseMirror — omit when the marker
      // was the entire text node (e.g. "[!NOTE]\n" followed by marked text).
      ...(stripped !== "" ? [{ ...firstText, text: stripped }] : []),
      ...first.content!.slice(1),
    ],
  };
  return { alertType, children: [restoredFirst, ...children.slice(1)] };
}

/**
 * Blockquote extension with GitHub alert support.
 *
 * Plain blockquotes behave exactly like the stock extension; quotes opening
 * with an alert marker get an `alertType` attr, GitHub-style styling hooks,
 * and marker-preserving serialisation.
 */
export const GitHubAlertBlockquote = Blockquote.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      alertType: {
        default: null,
        // Pasted/initial HTML can carry arbitrary attribute values — accept
        // only the five known types so renderHTML never sees garbage.
        parseHTML: (element) => {
          const raw = element.getAttribute("data-alert-type");
          return raw && (ALERT_TYPES as readonly string[]).includes(raw) ? raw : null;
        },
        renderHTML: (attributes) => {
          const alertType = attributes.alertType as AlertType | null;
          // Re-validate: attrs can also be set programmatically.
          if (!alertType || !(ALERT_TYPES as readonly string[]).includes(alertType)) return {};
          return {
            "data-alert-type": alertType,
            // Consumed by the CSS label (content: attr(data-alert-label)).
            "data-alert-label": ALERT_LABELS[alertType],
          };
        },
      },
    };
  },
  parseMarkdown: (token, helpers) => {
    const parseBlockChildren = helpers.parseBlockChildren ?? helpers.parseChildren;
    const { alertType, children } = extractAlert(parseBlockChildren(token.tokens || []));
    return helpers.createNode("blockquote", alertType ? { alertType } : undefined, children);
  },
  renderMarkdown: (node, h) => {
    if (!node.content) {
      return "";
    }
    const prefix = ">";
    const alertType = node.attrs?.alertType as AlertType | null;
    const result: string[] = [];
    node.content.forEach((child, index) => {
      let childContent = h.renderChild?.(child, index) ?? h.renderChildren([child]);
      if (index === 0 && alertType) {
        // Re-emit the marker the parser stripped, glued to the first
        // paragraph as in the common `> [!NOTE]\n> text` source shape.
        // (A standalone marker paragraph normalises to this form too —
        // identical rendering on GitHub.)
        const marker = `[!${alertType.toUpperCase()}]`;
        childContent = childContent === "" ? marker : `${marker}\n${childContent}`;
      }
      const lines = childContent.split("\n");
      const linesWithPrefix = lines.map((line) =>
        line.trim() === "" ? prefix : `${prefix} ${line}`,
      );
      result.push(linesWithPrefix.join("\n"));
    });
    return result.join(`\n${prefix}\n`);
  },
});
