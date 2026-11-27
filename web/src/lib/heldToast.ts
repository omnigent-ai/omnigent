// A toast whose action starts a change that takes a while, such as Undo or Try Again: it stays
// up, saying what's under way, until the change's outcome replaces it under the same id. Sonner
// would otherwise close it on the click, and the outcome would replace a toast already leaving.

import { toast } from "sonner";

/** Back to the Toaster's own timing, for a toast that replaces a held one. */
export const TIMED_TOAST = { duration: undefined, closeButton: undefined } as const;

/** Keep toast `id` up, saying `pending`, with nothing to click again while it runs. */
export function holdToast(
  event: { preventDefault: () => void } | undefined,
  id: string,
  pending: string,
): void {
  event?.preventDefault();
  // Sonner keeps an updated toast's fields: drop the clicked action, and what it said, while it runs.
  toast(pending, {
    id,
    duration: Number.POSITIVE_INFINITY,
    action: undefined,
    description: undefined,
  });
}

/** An action that sent nothing: its held toast goes, unless a run it waits on will replace it. */
export function settleHeldToast(id: string, stillRunning: boolean): void {
  if (!stillRunning) toast.dismiss(id);
}
