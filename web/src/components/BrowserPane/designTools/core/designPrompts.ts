// Chat messages for the Browser tab's design tools that lib/designModePrompt doesn't build
// (Annotate's is its design-mode message): Draw's and Live Edit's, and the page address they name.
import type { DesignModeElement } from "@/lib/designModePrompt";

/** A text edit Live Edit reports; every field comes from the page. */
export interface LiveEdit {
  before: string;
  after: string;
  element: DesignModeElement;
}

/**
 * The page's address as the agent is told it: its origin only. The path, query and fragment
 * can carry credentials no pattern tells from a route, and the screenshot shows the page.
 */
export function pageAddress(url: string): string {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "(unknown page)";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return `${parsed.protocol} page`;
  }
  return parsed.origin;
}

/** The Draw message: the user's note, and what the attached screenshot shows. */
export function buildDrawPrompt(note: string, url: string): string {
  const request = note.trim() || "Make the changes I marked on the attached screenshot.";
  return (
    `${request}\n\n---\n[Design Mode — marked-up screenshot of the browser preview]\n` +
    `Page: ${pageAddress(url)}\nThe attached screenshot shows the page with my marks drawn on it.\n---`
  );
}

// Characters JSON leaves literal that would still break a message line.
const LITERAL_BREAKS = /[\u007F-\u009F\u2028\u2029]/g;

/** Page text in a message, exactly: one escaped, quoted line. */
function quoted(value: unknown): string {
  const text = typeof value === "string" ? value : "";
  return JSON.stringify(text).replace(
    LITERAL_BREAKS,
    (char) => `\\u${char.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

/** An element as an edit line names it: `<tag>#id`, with only the characters those take. */
function elementLabel(element: DesignModeElement): string {
  const tag = typeof element.tag === "string" ? element.tag.replace(/[^a-z0-9-]/gi, "") : "";
  const id = typeof element.id === "string" ? element.id.replace(/[^\w#-]/g, "") : "";
  return `<${tag || "element"}>${id}`;
}

/** The Live Edit message: each changed text, before and after. */
export function buildLiveEditPrompt(edits: readonly LiveEdit[], url: string): string {
  const lines = edits.map(
    (edit, index) =>
      `${index + 1}. ${elementLabel(edit.element)}: ${quoted(edit.before)} → ${quoted(edit.after)}`,
  );
  return (
    "Make these text changes in the source; I made them in the browser preview.\n\n---\n" +
    `[Design Mode — live text edits in the browser preview]\nPage: ${pageAddress(url)}\n${lines.join("\n")}\n---`
  );
}
