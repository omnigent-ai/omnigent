// A base menu's choice can swap the review it sits in for another, which drops the focused
// trigger; focus then moves to the trigger that replaced it instead of falling to the page.

import { useEffect, useLayoutEffect, useRef } from "react";

import { BASE_MENU_TRIGGER_ATTRIBUTE } from "./DiffReviewBaseMenu";

const TRIGGER = `[${BASE_MENU_TRIGGER_ATTRIBUTE}]`;

/** The focused trigger and the elements around it, nearest first, as they were when it was focused. */
interface FocusedTrigger {
  trigger: HTMLElement;
  ancestors: HTMLElement[];
}

function noted(trigger: HTMLElement): FocusedTrigger {
  const ancestors: HTMLElement[] = [];
  for (let element = trigger.parentElement; element; element = element.parentElement) {
    ancestors.push(element);
  }
  return { trigger, ancestors };
}

/** Whether focus now sits somewhere the person chose, so it shouldn't be moved. */
function focusIsPlaced(): boolean {
  const active = document.activeElement;
  return !!active && active !== document.body && active.isConnected;
}

/**
 * Keeps focus on a base menu while the review holding it is replaced: once its focused trigger
 * is removed and focus fell to the page, the trigger now in its nearest remaining container
 * takes it. Call it in the component whose renders swap the reviews.
 */
export function useBaseMenuFocusHandoff(): void {
  const focused = useRef<FocusedTrigger | null>(null);
  useEffect(() => {
    const noteFocus = (event: FocusEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.matches(TRIGGER)) focused.current = noted(target);
      // The menu's own items keep the trigger noted; anywhere else lets it go.
      else if (!target?.closest('[role="menu"]')) focused.current = null;
    };
    // Focus leaving a trigger that's still on the page for nowhere is the person leaving it.
    const noteBlur = (event: FocusEvent) => {
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target === focused.current?.trigger && !event.relatedTarget && target.isConnected) {
        focused.current = null;
      }
    };
    document.addEventListener("focusin", noteFocus);
    document.addEventListener("focusout", noteBlur);
    return () => {
      document.removeEventListener("focusin", noteFocus);
      document.removeEventListener("focusout", noteBlur);
    };
  }, []);
  // Every render, since any render can swap the review that holds the trigger.
  useLayoutEffect(() => {
    const previous = focused.current;
    if (!previous || previous.trigger.isConnected) return;
    if (focusIsPlaced()) {
      focused.current = null;
      return;
    }
    const container = previous.ancestors.find((element) => element.isConnected);
    // Without a trigger yet, as between two reviews, a later render hands focus on.
    container?.querySelector<HTMLElement>(TRIGGER)?.focus();
  });
}
