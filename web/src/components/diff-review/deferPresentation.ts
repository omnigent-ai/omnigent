// When a review shows a change of the reader's rich diff choice: as a transition, so React
// renders it after the click that made it, in slices, rather than inside the click.
import { startTransition } from "react";

/** Schedules `apply`, a presentation update, as non-urgent work. */
export function deferPresentation(apply: () => void): void {
  startTransition(apply);
}
