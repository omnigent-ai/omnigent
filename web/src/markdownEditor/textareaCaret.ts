// Where a textarea draws the character at an offset, on screen: a hidden copy with the same text
// and box lays the text out the same way, and a marker at the offset gives its place.

const MIRRORED_STYLES = [
  "boxSizing",
  "width",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "fontFamily",
  "fontSize",
  "fontStyle",
  "fontVariant",
  "fontWeight",
  "fontStretch",
  "letterSpacing",
  "lineHeight",
  "tabSize",
  "textIndent",
  "textTransform",
  "wordSpacing",
] as const;

/** The box of the character at `offset` in `textarea`, in viewport coordinates. */
export function textareaCaretRect(textarea: HTMLTextAreaElement, offset: number): DOMRect {
  const document = textarea.ownerDocument;
  const style = getComputedStyle(textarea);
  const mirror = document.createElement("div");
  for (const property of MIRRORED_STYLES) mirror.style[property] = style[property];
  Object.assign(mirror.style, {
    position: "absolute",
    visibility: "hidden",
    top: "0",
    left: "-9999px",
    whiteSpace: "pre-wrap",
    overflowWrap: "break-word",
    overflow: "hidden",
  });
  mirror.textContent = textarea.value.slice(0, offset);
  const marker = document.createElement("span");
  // A character of its own, so a marker at a line's end still has a height.
  marker.textContent = textarea.value[offset] ?? "​";
  mirror.append(marker);
  document.body.append(mirror);
  const box = textarea.getBoundingClientRect();
  const left = box.left + marker.offsetLeft - textarea.scrollLeft;
  const top = box.top + marker.offsetTop - textarea.scrollTop;
  const height = marker.offsetHeight || Number.parseFloat(style.lineHeight) || 16;
  mirror.remove();
  return new DOMRect(left, top, 1, height);
}
