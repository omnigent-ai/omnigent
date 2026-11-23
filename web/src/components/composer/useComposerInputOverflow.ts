// Whether a composer's draft has outgrown its box, for either input engine: the input area
// scrolls (quote blocks), or the textarea or CodeMirror scroller inside it does.

import { useLayoutEffect, useState, type RefObject } from "react";

/** Inputs that scroll on their own once the draft passes the box's cap. */
const SCROLLING_INPUTS = "textarea, .cm-scroller";

/** Whether anything in `area` holds more draft than it shows. */
export function composerInputOverflows(area: HTMLElement): boolean {
  if (area.scrollHeight > area.clientHeight + 1) return true;
  for (const input of area.querySelectorAll<HTMLElement>(SCROLLING_INPUTS)) {
    if (input.scrollHeight > input.clientHeight + 1) return true;
  }
  return false;
}

/** Live {@link composerInputOverflows} for the area at `areaRef`, while `enabled`. */
export function useComposerInputOverflow(
  areaRef: RefObject<HTMLElement | null>,
  enabled: boolean,
): boolean {
  const [overflowing, setOverflowing] = useState(false);
  useLayoutEffect(() => {
    const area = areaRef.current;
    if (!enabled || !area) {
      setOverflowing(false);
      return;
    }
    let frame = 0;
    // Edits, the textarea's auto-grow and engine switches all touch the area's DOM.
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => setOverflowing(composerInputOverflows(area)));
    };
    setOverflowing(composerInputOverflows(area));
    const resize = new ResizeObserver(measure);
    resize.observe(area);
    // An input can shrink while the area keeps its size, so each scrolling input is watched
    // too, and an engine switch or a new quote that replaces one is watched in its place.
    let inputs: HTMLElement[] = [];
    const watchInputs = () => {
      const current = Array.from(area.querySelectorAll<HTMLElement>(SCROLLING_INPUTS));
      for (const input of inputs) if (!current.includes(input)) resize.unobserve(input);
      for (const input of current) if (!inputs.includes(input)) resize.observe(input);
      inputs = current;
    };
    watchInputs();
    const mutations = new MutationObserver(() => {
      watchInputs();
      measure();
    });
    mutations.observe(area, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
    area.addEventListener("input", measure, true);
    return () => {
      cancelAnimationFrame(frame);
      mutations.disconnect();
      resize.disconnect();
      area.removeEventListener("input", measure, true);
    };
  }, [areaRef, enabled]);
  return overflowing;
}
