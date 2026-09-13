// Pure helpers for guarding a Radix Dialog's outside-dismiss against nested
// dropdown / Select / popover portals.
//
// A modal Dialog closes on any outside-interaction, but the dropdowns nested in
// it (agent picker, host Select, …) portal their content OUTSIDE the
// DialogContent subtree — so their own dismiss (pick an option, or click the
// modal body while open) bubbles up as an "outside interaction" and would
// close the whole Dialog. These helpers let a dialog swallow only those narrow
// cases while still closing on a real backdrop click or Escape.
//
// Kept dependency-free (no component imports) so any dialog can use it without
// dragging in unrelated module graphs.

/**
 * True when an event target lives inside a Radix popper / Select portal (which
 * renders outside the DialogContent subtree). Used to distinguish a click that
 * merely closes a nested dropdown from a genuine outside-click on the backdrop.
 */
export function isInsidePopper(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    target.closest(
      [
        "[data-radix-popper-content-wrapper]",
        '[data-slot="dropdown-menu-content"]',
        '[data-slot="popover-content"]',
        '[data-slot="select-content"]',
        '[role="listbox"]',
      ].join(", "),
    ) !== null
  );
}

/** True when the event target is the Dialog's backdrop overlay itself. A real
 *  backdrop click must always dismiss, so the guard lets it through. */
export function isBackdropOverlay(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('[data-slot="dialog-overlay"]') !== null;
}

/**
 * Pure decision for whether to SWALLOW the Dialog's outside-dismiss. Returns
 * true → preventDefault (dialog stays open); false → let it dismiss.
 *
 * A genuine backdrop-overlay click ALWAYS dismisses (returns false), even during
 * the grace window — this is the fix for backdrop-click-to-close being swallowed.
 * Otherwise we swallow only the narrow nested-dropdown cases: a dropdown
 * currently open, the trailing focus-outside within `graceMs` of a dropdown
 * closing, or a click that landed inside portalled dropdown content. Pure so
 * it's unit-testable without Radix's portal machinery.
 */
export function shouldGuardDialogDismiss(
  target: EventTarget | null,
  opts: { selectOpen: boolean; msSinceSelectClose: number; graceMs?: number },
): boolean {
  if (isBackdropOverlay(target)) return false;
  const graceMs = opts.graceMs ?? 150;
  return opts.selectOpen || opts.msSinceSelectClose < graceMs || isInsidePopper(target);
}
