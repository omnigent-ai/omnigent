/** Design-mode prompt helpers.
 *
 *  Pure functions used by AppShell's element-prompt-submit listener to turn a
 *  picked element + user prompt into a chat message. Kept out of the component
 *  so they're unit-testable without a React render.
 *
 *  There is NO backend design-edit route: the element prompt is
 *  sent as an ordinary user message through `chatStore.send`, with the cropped
 *  element screenshot riding along as an attachment File. So this is a pure
 *  client affordance — see AppShell + BrowserPane. */

/** The element info the in-page picker emits (mirrors DESIGN_MODE_SCRIPT's
 *  getElementInfo). All fields optional — an older injected script or an
 *  unusual element may omit some. */
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

/** Max length any single element field may contribute to the prompt block.
 *  The element.* fields originate from the browsed page's DOM, so they are
 *  UNTRUSTED — a hostile page can stuff arbitrary text (or newlines that would
 *  break the fenced `[Design Mode — …]` block structure) into id/class/text.
 *  Clamp so a huge attribute can't blow up the message. */
const FIELD_MAX = 200;

/** Characters stripped before a field enters the prompt block: C0 controls
 *  (U+0000-U+001F, incl. newline and tab), DEL + C1 (U+007F-U+009F), and the
 *  Unicode line/paragraph separators (U+2028 / U+2029). Any of these could
 *  forge extra block lines or break the `---` fences. Written with \u escapes
 *  (not literal bytes) so the source itself contains no line terminators. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001F\u007F-\u009F\u2028\u2029]/g;

/** Sanitize an untrusted element field before it templates into the prompt:
 *  strip control characters and newlines, collapse whitespace, and clamp
 *  length. Returns "" for nullish input so callers can treat "" as "absent". */
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
 * Build the chat message text for a design-mode submit: the user's prompt
 * followed by a fenced `[Design Mode — …]` context block describing the picked
 * element. The block shape is kept stable so any downstream parser that strips
 * it from the rendered bubble stays compatible.
 *
 * Every element-derived field is passed through `sanitizeField` first — the
 * element info comes from the browsed page's DOM and is untrusted, so it must
 * not be able to forge extra block lines or break the `---` fences. The user's
 * own `prompt` is trusted (they typed it) and passes through verbatim.
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
 * Convert a base64 data URL (the cropped element screenshot the main process
 * captured) into a `File` so it can ride the normal chat-send attachment path.
 * Returns null if the input isn't a usable `data:image/...;base64,...` URL.
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
