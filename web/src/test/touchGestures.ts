/** Dispatch a one-finger 120px downward swipe on `target` for jsdom tests. */
export function swipeDown(target: Element) {
  for (const [type, y] of [
    ["touchstart", 200],
    ["touchmove", 240],
    ["touchmove", 320],
    ["touchend", 320],
  ] as const) {
    const event = new Event(type, { bubbles: true });
    Object.defineProperty(event, "touches", {
      value: type === "touchend" ? [] : [{ clientX: 100, clientY: y }],
    });
    target.dispatchEvent(event);
  }
}
