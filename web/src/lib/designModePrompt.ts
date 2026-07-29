/** Design-mode prompt helpers — pure functions (unit-testable, no React) that
 *  turn a picked element + prompt into a chat message. There is NO backend
 *  route: it's sent as an ordinary user message via `chatStore.send` with the
 *  cropped screenshot as an attachment. */

/** Element info the in-page picker emits. All fields optional (an older script
 *  or unusual element may omit some). */
export interface DesignModeElement {
  tag?: string;
  id?: string;
  classes?: string;
  text?: string;
  testId?: string;
  ariaLabel?: string;
  role?: string;
  component?: string | null;
}

/** Clamp per element field: element.* comes from the page DOM (UNTRUSTED), so a
 *  hostile page can't blow up the message with a huge attribute. */
const FIELD_MAX = 200;

/** Control chars + line/paragraph separators stripped from fields, so untrusted
 *  DOM text can't forge extra `[Design Mode — …]` block lines or break the `---`
 *  fences. \u escapes keep this source free of literal line terminators. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F\u2028\u2029]/g;

/** Sanitize an untrusted element field: strip control chars, collapse
 *  whitespace, clamp length. Returns "" for nullish (callers treat "" as absent). */
function sanitizeField(value: string | null | undefined): string {
  if (typeof value !== "string") return "";
  return value.replace(CONTROL_CHARS, " ").replace(/\s+/g, " ").trim().slice(0, FIELD_MAX);
}

/** Human-readable element name: prefer the React component, else the tag. Both
 *  are sanitized — they come from the page DOM. */
function displayName(el: DesignModeElement): string {
  const component = sanitizeField(el.component);
  if (component) return `<${component}>`;
  const tag = sanitizeField(el.tag);
  return `<${tag || "element"}>`;
}

/** Best-effort CSS selector for the element, in precedence order:
 *  data-testid → id → tag+classes. All parts are sanitized (untrusted DOM). */
function selectorFor(el: DesignModeElement): string {
  const testId = sanitizeField(el.testId);
  if (testId) return `[data-testid="${testId}"]`;
  const id = sanitizeField(el.id);
  if (id) return id; // already carries the leading '#'
  return `${sanitizeField(el.tag)}${sanitizeField(el.classes)}`;
}

/**
 * Build the design-mode message: the user's prompt plus a stable fenced
 * `[Design Mode — …]` block describing the element. Every element field is
 * sanitized (untrusted DOM); the user's `prompt` is trusted and verbatim.
 *
 * @param element the picked element info
 * @param prompt the user's typed instruction
 * @returns the full message text to send
 */
export function buildDesignModePrompt(element: DesignModeElement, prompt: string): string {
  const text = sanitizeField(element.text);
  const ariaLabel = sanitizeField(element.ariaLabel);
  const role = sanitizeField(element.role);
  const ctx = [
    `[Design Mode — modify this element in the browser preview]`,
    `Element: ${displayName(element)}`,
    `CSS selector: ${selectorFor(element)}`,
    text ? `Text: "${text}"` : "",
    ariaLabel ? `Aria-label: "${ariaLabel}"` : "",
    role ? `Role: ${role}` : "",
  ]
    .filter(Boolean)
    .join("\n");
  return `${prompt}\n\n---\n${ctx}\n---`;
}

/**
 * Convert a base64 data URL (the cropped screenshot) into a `File` for the
 * normal chat-send attachment path. Null if not a usable data: URL.
 *
 * @param dataUrl e.g. "data:image/png;base64,iVBORw0K…"
 * @param filename the attachment filename
 */
export function dataUrlToFile(dataUrl: string | null | undefined, filename: string): File | null {
  if (typeof dataUrl !== "string") return null;
  const match = /^data:([^;,]+)(;base64)?,(.*)$/s.exec(dataUrl);
  if (!match) return null;
  const mime = match[1] || "image/png";
  const isBase64 = !!match[2];
  const data = match[3] ?? "";
  try {
    const raw = isBase64 ? atob(data) : decodeURIComponent(data);
    // Back the bytes with a plain ArrayBuffer (not the SharedArrayBuffer-union
    // TS infers for a bare Uint8Array) so the BlobPart type checks cleanly.
    const buffer = new ArrayBuffer(raw.length);
    const bytes = new Uint8Array(buffer);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return new File([buffer], filename, { type: mime });
  } catch {
    return null;
  }
}
