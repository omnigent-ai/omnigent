// Read back a design-tool message (lib/designModePrompt's and designPrompts' `[Design Mode — …]`
// block) so the chat can show it as a compact card instead of raw Markdown. What the agent
// receives is unchanged.

export type DesignRequestKind = "element" | "drawing" | "edits";

/** One of Live Edit's text changes, as its message names it. */
export interface DesignRequestEdit {
  element: string;
  before: string;
  after: string;
}

export interface DesignRequest {
  kind: DesignRequestKind;
  /** What the user typed (Live Edit's is fixed copy, which its card doesn't show). */
  request: string;
  /** The picked element, e.g. `p#blurb`; element requests only. */
  element?: string;
  /** The React component design mode named, e.g. `CheckoutButton`; element requests only. */
  component?: string;
  /** The element's text; element requests only. */
  elementText?: string;
  /** The element's accessible label and role; element requests only. */
  ariaLabel?: string;
  role?: string;
  /** The page's host, e.g. `localhost:5173`. */
  page?: string;
  /** The block says no screenshot was attached; element requests only. */
  noScreenshot?: boolean;
  /** Live Edit's changes; a collapsed card keeps only the first ones. */
  edits?: DesignRequestEdit[];
  /** How many changes the message has in all, when `edits` holds fewer. */
  editCount?: number;
}

/** `text`, at most `limit` code points of it. */
function clip(text: string, limit: number): string {
  if (text.length <= limit) return text;
  return Array.from(text.slice(0, limit * 2))
    .slice(0, limit)
    .join("");
}

/**
 * `request` as a collapsed message shows it: at most `limit` characters of its words,
 * element text and edits in all (later edits dropped), so the card's size stays bounded.
 */
export function clipDesignRequest(request: DesignRequest, limit: number): DesignRequest {
  const length = (text: string) => [...text].length;
  const words = clip(request.request, limit);
  const { elementText, ariaLabel, ...rest } = request;
  let left = Math.max(limit - length(words), 0);
  const clipped: DesignRequest = { ...rest, request: words };
  if (elementText !== undefined && length(elementText) > left) {
    // A cut text ends in "…"; a label matching the whole text stays hidden, as unclipped.
    clipped.elementText = `${clip(elementText, Math.max(left - 1, 0))}…`;
    if (ariaLabel !== undefined && ariaLabel !== elementText) clipped.ariaLabel = ariaLabel;
    left = 0;
  } else {
    if (elementText !== undefined) clipped.elementText = elementText;
    if (ariaLabel !== undefined) clipped.ariaLabel = ariaLabel;
    left -= elementText === undefined ? 0 : length(elementText);
  }
  if (request.edits) {
    // Live Edit's changes share what's left, in order, until it runs out.
    const take = (text: string) => {
      const kept = clip(text, left);
      left -= length(kept);
      return kept;
    };
    const edits: DesignRequestEdit[] = [];
    for (const edit of request.edits) {
      if (left <= 0) break;
      edits.push({ element: edit.element, before: take(edit.before), after: take(edit.after) });
    }
    clipped.edits = edits;
    clipped.editCount = request.editCount ?? request.edits.length;
  }
  return clipped;
}

/** A card's note when the agent got the request without its screenshot. */
export const NO_SCREENSHOT_NOTE = "Sent without a screenshot (it couldn't be taken).";

/** What a design request was about, as one line: the element and its text, or the page. */
export function designSubject(request: DesignRequest): string {
  if (request.kind === "drawing") return request.page ? `Drawing on ${request.page}` : "Drawing";
  if (request.kind === "edits") {
    const total = request.editCount ?? request.edits?.length ?? 0;
    const count = `${total} ${total === 1 ? "edit" : "edits"}`;
    return request.page ? `${count} on ${request.page}` : count;
  }
  const parts = [request.component ? `<${request.component}>` : "", request.element ?? "element"];
  if (request.elementText) parts.push(`"${request.elementText}"`);
  // An icon-only control is told apart by its accessible name.
  if (request.ariaLabel && request.ariaLabel !== request.elementText) {
    parts.push(`aria-label "${request.ariaLabel}"`);
  }
  if (request.role) parts.push(`role ${request.role}`);
  if (request.page) parts.push(`on ${request.page}`);
  return parts.filter(Boolean).join(" ");
}

const OPENING = "\n\n---\n[Design Mode — ";
const CLOSING = "\n---";
const ELEMENT_HEADER = "modify this element in the browser preview";
const DRAWING_HEADER = "marked-up screenshot of the browser preview";
const EDITS_HEADER = "live text edits in the browser preview";
// One of Live Edit's lines: `1. <h1>#title: "Old" → "New"`, its texts JSON strings.
const EDIT_LINE = /^\d+\. (.+?): (".*") → (".*")$/;
// An HTML tag as the picker reports it (lowercase); anything else is a component's name.
const TAG = /^[a-z][a-z0-9-]*$/;

function field(lines: string[], name: string): string | undefined {
  const line = lines.find((l) => l.startsWith(`${name}: `));
  return line?.slice(name.length + 2);
}

function unquote(value: string | undefined): string | undefined {
  if (!value) return undefined;
  return value.length > 1 && value.startsWith('"') && value.endsWith('"')
    ? value.slice(1, -1)
    : value;
}

function elementRequest(request: string, lines: string[]): DesignRequest | null {
  const selector = field(lines, "CSS selector");
  const name = field(lines, "Element");
  if (selector === undefined || name === undefined) return null;
  // `<element>` is the prompt's stand-in for an element whose tag wasn't known.
  const bare = name === "<element>" ? "" : name.replace(/^<|>$/g, "");
  // `p` and `#blurb` read as `p#blurb`; a component's name stays apart from the selector.
  const tag = TAG.test(bare) ? bare : "";
  const element = selector && !selector.startsWith(tag) ? `${tag}${selector}` : selector || tag;
  return {
    kind: "element",
    request,
    element: element || undefined,
    component: tag || !bare ? undefined : bare,
    elementText: unquote(field(lines, "Text")),
    ariaLabel: unquote(field(lines, "Aria-label")),
    role: field(lines, "Role") || undefined,
    page: shownPage(field(lines, "Page")),
    ...(field(lines, "Screenshot")?.startsWith("none") ? { noScreenshot: true } : {}),
  };
}

/** A block's `Page` as a card shows it: its host only, whatever an older message saved. */
function shownPage(page: string | undefined): string | undefined {
  if (!page) return undefined;
  try {
    const url = new URL(page);
    return url.protocol === "http:" || url.protocol === "https:" ? url.host : undefined;
  } catch {
    return undefined;
  }
}

function drawingRequest(request: string, lines: string[]): DesignRequest | null {
  const page = field(lines, "Page");
  if (page === undefined) return null;
  return { kind: "drawing", request, page: shownPage(page) };
}

/** A JSON string from an edit line, or the line's text as it is. */
function parseQuoted(value: string): string {
  try {
    const parsed: unknown = JSON.parse(value);
    return typeof parsed === "string" ? parsed : value;
  } catch {
    return value;
  }
}

function editsRequest(request: string, lines: string[]): DesignRequest | null {
  const page = field(lines, "Page");
  if (page === undefined) return null;
  const edits = lines.flatMap((line) => {
    const edit = line.match(EDIT_LINE);
    if (!edit) return [];
    return [
      {
        element: edit[1]!.replace(/^<|>/g, ""),
        before: parseQuoted(edit[2]!),
        after: parseQuoted(edit[3]!),
      },
    ];
  });
  return { kind: "edits", request, page: shownPage(page), edits };
}

/**
 * The design request a message carries, or null when it's an ordinary message. Only the
 * block a tool appends at the very end counts, so one the user quoted above stays words.
 */
export function parseDesignRequest(text: string): DesignRequest | null {
  const message = text.trimEnd();
  if (!message.endsWith(CLOSING)) return null;
  const start = message.lastIndexOf(OPENING);
  if (start === -1) return null;
  const block = message.slice(start + OPENING.length, message.length - CLOSING.length);
  const headerEnd = block.indexOf("]\n");
  if (headerEnd === -1) return null;
  const header = block.slice(0, headerEnd);
  const lines = block.slice(headerEnd + 2).split("\n");
  const request = message.slice(0, start).trim();
  if (header === ELEMENT_HEADER) return elementRequest(request, lines);
  if (header === DRAWING_HEADER) return drawingRequest(request, lines);
  if (header === EDITS_HEADER) return editsRequest(request, lines);
  return null;
}
