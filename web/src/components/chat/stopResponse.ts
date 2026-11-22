// When the open conversation's response can be stopped, as Escape in the composer stops it.

/** A turn is running and the viewer may send to the session. */
export function canStopResponse({ working, readOnly }: { working: boolean; readOnly: boolean }) {
  return working && !readOnly;
}
