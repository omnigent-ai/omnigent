// A key for a dialog's content that changes on each opening. A dialog reopened during its exit
// animation then mounts fresh content, so Radix autofocuses it as on any opening, instead of
// leaving focus where the closing content had it, such as on a row the user clicked.

import { useState } from "react";

/** Counts the dialog's openings, including one during the last one's exit. */
export function useDialogOpeningKey(open: boolean): number {
  const [openings, setOpenings] = useState({ open, count: 0 });
  if (openings.open !== open) setOpenings({ open, count: openings.count + (open ? 1 : 0) });
  return openings.count;
}
