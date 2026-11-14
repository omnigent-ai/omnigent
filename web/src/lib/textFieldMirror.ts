/**
 * A detached, hidden <div> that lays text out like ``field``, for measuring where
 * its caret sits; text fields have no caret-rect API. The caller fills, attaches
 * and removes it. Two details make the mirror wrap *identically* to the field
 * (earlier bugs made it wrap a word early):
 *   - the font is copied via the `font` SHORTHAND; copying individual longhand
 *     properties lets an inherited font-stretch/variation widen the mirror's
 *     text and wrap it early;
 *   - getComputedStyle().width is the content-box width, so the mirror uses
 *     box-sizing:content-box with width = clientWidth - horizontal padding and
 *     zero padding/border, rather than copying width onto a border-box element.
 */
export function createTextFieldMirror(
  field: HTMLTextAreaElement | HTMLInputElement,
  style: CSSStyleDeclaration,
): HTMLDivElement {
  const padLeft = parseFloat(style.paddingLeft) || 0;
  const padRight = parseFloat(style.paddingRight) || 0;
  const mirror = field.ownerDocument.createElement("div");
  // The `font` shorthand carries family/size/weight/style/line-height together
  // and resets the rest, so the mirror's glyph metrics match the field exactly.
  mirror.style.font = style.font;
  mirror.style.letterSpacing = style.letterSpacing;
  mirror.style.wordSpacing = style.wordSpacing;
  mirror.style.textTransform = style.textTransform;
  mirror.style.textIndent = style.textIndent;
  mirror.style.tabSize = style.tabSize;
  mirror.style.direction = style.direction;
  mirror.style.position = "absolute";
  mirror.style.top = "0";
  mirror.style.left = "0";
  mirror.style.boxSizing = "content-box";
  mirror.style.width = `${field.clientWidth - padLeft - padRight}px`;
  mirror.style.height = "auto";
  mirror.style.padding = "0";
  mirror.style.border = "0";
  mirror.style.visibility = "hidden";
  mirror.style.whiteSpace = field instanceof HTMLInputElement ? "pre" : "pre-wrap";
  mirror.style.wordWrap = "break-word";
  mirror.style.overflowWrap = "break-word";
  return mirror;
}
