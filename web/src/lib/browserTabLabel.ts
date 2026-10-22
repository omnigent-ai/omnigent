/** Label for a browser tab that has not loaded a page yet. */
export const NEW_BROWSER_TAB_LABEL = "New tab";

/** Longest manual browser-tab name the rename field accepts. */
export const BROWSER_TAB_NAME_MAX_CHARS = 80;

/** Longest URL label; the full URL stays in the tab tooltip. */
const URL_LABEL_MAX_CHARS = 60;

// Bidi, zero-width and control characters could make a label read as something else.
const SPOOFABLE_CHARS = /[\p{Cc}\p{Cf}]/u;
const SPOOFABLE_CHARS_GLOBAL = /[\p{Cc}\p{Cf}]/gu;

// Schemes whose labels come from the parsed URL; others show as raw text.
const PARSED_SCHEMES = new Set(["http", "https", "file", "about"]);
const URL_SCHEME = /^([a-z][a-z\d+.-]*):/i;

/** First `max` code points of `text`, so an emoji is never split. */
export function truncateCodePoints(text: string, max: number): string {
  // A code point is at most two UTF-16 units; bound the work for huge data: URLs.
  const prefix = text.slice(0, max * 2);
  const chars = Array.from(prefix);
  return chars.length > max ? chars.slice(0, max).join("") : prefix;
}

function clampUrlLabel(text: string): string {
  const cut = truncateCodePoints(text, URL_LABEL_MAX_CHARS);
  return cut === text ? text : `${cut}…`;
}

/** Raw text as a label: bounded first, with disguising characters percent-encoded. */
function rawUrlLabel(text: string): string {
  const bounded = truncateCodePoints(text, URL_LABEL_MAX_CHARS + 1);
  return clampUrlLabel(bounded.replace(SPOOFABLE_CHARS_GLOBAL, (char) => encodeURIComponent(char)));
}

/** A manual name as stored: trimmed and capped; blank means no name. */
export function normalizeBrowserTabName(name: string): string {
  return truncateCodePoints(name.trim(), BROWSER_TAB_NAME_MAX_CHARS).trim();
}

function decodeSegment(segment: string): string {
  try {
    const decoded = decodeURIComponent(segment);
    // An encoded slash stays encoded so one segment cannot pose as several.
    return SPOOFABLE_CHARS.test(decoded) || decoded.includes("/") ? segment : decoded;
  } catch {
    return segment;
  }
}

/**
 * Short label for a page URL: host plus at most the last two path segments,
 * e.g. `github.com/…/pull/123` or `localhost:3000/settings`. Query and hash
 * are dropped; a blank or `about:blank` URL reads as a new tab.
 */
export function browserUrlLabel(url: string | null | undefined): string {
  const trimmed = url?.trim() ?? "";
  if (trimmed === "" || trimmed === "about:blank") return NEW_BROWSER_TAB_LABEL;
  // Skip parsing raw-text schemes, which can be huge (data: URLs).
  const scheme = URL_SCHEME.exec(trimmed.slice(0, 32))?.[1]?.toLowerCase();
  if (scheme && !PARSED_SCHEMES.has(scheme)) return rawUrlLabel(trimmed);
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return rawUrlLabel(trimmed);
  }
  if (parsed.protocol === "about:" && parsed.pathname === "blank") return NEW_BROWSER_TAB_LABEL;
  const segments = parsed.pathname.split("/").filter(Boolean).map(decodeSegment);
  if (parsed.protocol === "http:" || parsed.protocol === "https:") {
    const host = parsed.host.replace(/^www\./, "");
    if (segments.length === 0) return clampUrlLabel(host);
    const tail = segments.slice(-2).join("/");
    return clampUrlLabel(segments.length > 2 ? `${host}/…/${tail}` : `${host}/${tail}`);
  }
  const last = segments.at(-1);
  if (parsed.protocol === "file:" && last) return clampUrlLabel(last);
  return rawUrlLabel(trimmed);
}

/** Starting value of the rename field: the label cut to the name limit. */
export function browserTabNameSeed(label: string): string {
  return truncateCodePoints(label, BROWSER_TAB_NAME_MAX_CHARS);
}

/** The visible name of a browser tab: its manual name, else its URL label. */
export function browserTabLabel(
  manualName: string | null | undefined,
  url: string | null | undefined,
): string {
  const name = manualName?.trim() ?? "";
  return name !== "" ? name : browserUrlLabel(url);
}
