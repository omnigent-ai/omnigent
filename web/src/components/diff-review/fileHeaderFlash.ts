// Briefly tints a file's header after a tree pick scrolls the review to it, so
// the eye lands on the file that was clicked. The header lives in the file's
// shadow root, so the flash is an attribute its injected styles animate.

export const FILE_HEADER_FLASH_ATTRIBUTE = "data-review-flash";
export const FILE_HEADER_FLASH_MS = 1200;

/** Styles the shadow roots need for the flash; document styles don't reach them. */
export const FILE_HEADER_FLASH_CSS = `
  @keyframes review-file-header-flash {
    0%, 35% { background-color: color-mix(in oklch, var(--warning) 22%, transparent); }
  }
  [data-diffs-header][${FILE_HEADER_FLASH_ATTRIBUTE}] {
    animation: review-file-header-flash ${FILE_HEADER_FLASH_MS}ms ease-out;
  }
`;

const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>();

/** Flashes the header of a rendered file; false while its header isn't rendered yet. */
export function flashFileHeader(renderedItem: HTMLElement): boolean {
  const header = (renderedItem.shadowRoot ?? renderedItem).querySelector("[data-diffs-header]");
  if (!header) return false;
  // Restarting the animation for a second pick of the same file needs a fresh attribute.
  header.removeAttribute(FILE_HEADER_FLASH_ATTRIBUTE);
  void (header as HTMLElement).offsetWidth;
  header.setAttribute(FILE_HEADER_FLASH_ATTRIBUTE, "");
  clearTimeout(timers.get(header));
  timers.set(
    header,
    setTimeout(() => header.removeAttribute(FILE_HEADER_FLASH_ATTRIBUTE), FILE_HEADER_FLASH_MS),
  );
  return true;
}
