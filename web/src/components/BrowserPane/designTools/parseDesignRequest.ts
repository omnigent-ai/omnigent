// Read back a design-tool message (lib/designModePrompt's and designPrompts' `[Design Mode — …]`
// block) so the chat can show it as a compact card instead of raw Markdown. What the agent
// receives is unchanged.

export type DesignRequestKind = "element" | "drawing";

export interface DesignRequest {
  kind: DesignRequestKind;
  /** What the user typed. */
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
}

/** `text`, at most `limit` code points of it. */
function clip(text: string, limit: number): string {
  if (text.length <= limit) return text;
  return Array.from(text.slice(0, limit * 2))
    .slice(0, limit)
    .join("");
}

/**
 * `request` as a collapsed message shows it: at most `limit` characters of its words and
 * element text in all, so the card's size is bounded however much the message carries.
 */
export function clipDesignRequest(request: DesignRequest, limit: number): DesignRequest {
  const words = clip(request.request, limit);
  const { elementText, ariaLabel, ...rest } = request;
  const left = Math.max(limit - [...words].length, 0);
  if (elementText === undefined || [...elementText].length <= left) {
    return { ...request, request: words };
  }
  return {
    ...rest,
    request: words,
    // A cut text ends in "…"; a label matching the whole text stays hidden, as unclipped.
    elementText: `${clip(elementText, Math.max(left - 1, 0))}…`,
    ...(ariaLabel !== undefined && ariaLabel !== elementText ? { ariaLabel } : {}),
  };
}

/** A card's note when the agent got the request without its screenshot. */
export const NO_SCREENSHOT_NOTE = "Sent without a screenshot (it couldn't be taken).";

/** What a design request was about, as one line: the element and its text, or the page. */
export function designSubject(request: DesignRequest): string {
  if (request.kind === "drawing") return request.page ? `Drawing on ${request.page}` : "Drawing";
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
  return null;
}
