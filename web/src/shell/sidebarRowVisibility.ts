// Whether a sidebar row is fully on screen, so revealing the active row leaves a visible list alone.

/** Whether the row lies entirely inside its nearest scrolling ancestor (or the viewport). */
export function isRowFullyVisible(row: HTMLElement): boolean {
  let scroller = row.parentElement;
  while (scroller && !/auto|scroll/.test(getComputedStyle(scroller).overflowY)) {
    scroller = scroller.parentElement;
  }
  const bounds = scroller?.getBoundingClientRect() ?? { top: 0, bottom: window.innerHeight };
  const rect = row.getBoundingClientRect();
  return rect.top >= bounds.top && rect.bottom <= bounds.bottom;
}
