// GitHub's `> [!NOTE]`-style alerts in rendered PR Markdown. A quote is an alert when its first
// block is a paragraph whose first line is just the marker, as on GitHub; everything after the
// marker, in that paragraph and in the blocks that follow, stays in the alert. Any other quote,
// such as one opening with a list or one nested in another block, stays an ordinary quote.

import type { Element, ElementContent, Root, RootContent } from "hast";

const ALERT_TITLES = {
  note: "Note",
  tip: "Tip",
  important: "Important",
  warning: "Warning",
  caution: "Caution",
} as const;

type AlertKind = keyof typeof ALERT_TITLES;

/** The marker alone on its line: then a newline, or the end of its text node (checked below). */
const MARKER = /^\s*\[!(note|tip|important|warning|caution)\][ \t]*(?:\r?\n|$)/i;

function isBlank(node: ElementContent): boolean {
  return node.type === "text" && node.value.trim() === "";
}

/** `quote` as GitHub's alert markup, or null when it isn't an alert. */
function alertFor(quote: Element): Element | null {
  const firstIndex = quote.children.findIndex((child) => !isBlank(child));
  const first = quote.children[firstIndex];
  if (first?.type !== "element" || first.tagName !== "p") return null;
  const [lead, ...rest] = first.children;
  if (lead?.type !== "text") return null;
  const match = MARKER.exec(lead.value);
  if (!match) return null;

  const firstLine: ElementContent[] = [...rest];
  const after = lead.value.slice(match[0].length);
  const [opening, next] = firstLine;
  const endedWithNewline = /\n$/.test(match[0]);
  if (after !== "") firstLine.unshift({ type: "text", value: after });
  else if (!endedWithNewline && opening?.type === "element" && opening.tagName === "br") {
    // A hard break (two spaces or a backslash) ended the marker's line instead.
    firstLine.shift();
    if (next?.type === "text") firstLine[0] = { ...next, value: next.value.replace(/^\r?\n/, "") };
  } else if (!endedWithNewline && opening !== undefined) {
    // Emphasis, code or a link right after the marker is text on its line: not an alert.
    return null;
  }
  const body: ElementContent[] = [];
  if (firstLine.some((child) => !isBlank(child))) {
    body.push({ type: "element", tagName: "p", properties: {}, children: firstLine });
  }
  body.push(...quote.children.slice(firstIndex + 1));
  // A marker with nothing after it stays a quote, as on GitHub.
  if (!body.some((child) => !isBlank(child))) return null;

  const kind = match[1]!.toLowerCase() as AlertKind;
  return {
    type: "element",
    tagName: "div",
    properties: { className: ["markdown-alert", `markdown-alert-${kind}`] },
    children: [
      {
        type: "element",
        tagName: "p",
        properties: { className: ["markdown-alert-title"] },
        children: [{ type: "text", value: ALERT_TITLES[kind] }],
      },
      ...body,
    ],
  };
}

// GitHub renders alerts only at the top level: one inside a quote, list or other block stays a quote.
function replaceAlerts(children: RootContent[]): void {
  for (let index = 0; index < children.length; index += 1) {
    const child = children[index]!;
    if (child.type !== "element" || child.tagName !== "blockquote") continue;
    const alert = alertFor(child);
    if (alert) children[index] = alert;
  }
}

/** The rehype plugin. It adds its own markup, so it runs after sanitizing. */
export function rehypeGitHubAlerts() {
  return (tree: Root) => replaceAlerts(tree.children);
}
