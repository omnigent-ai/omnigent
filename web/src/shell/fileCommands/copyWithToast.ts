// Copying text for a file action, and saying how it went.

import { toast } from "sonner";
import { copyText } from "@/lib/clipboard";

/** Copies `text`, then toasts `copied`, or `failed` when the clipboard refused. */
export function copyWithToast(text: string, copied: string, failed: string) {
  void copyText(text).then(
    () => toast.success(copied),
    () => toast.error(failed),
  );
}
