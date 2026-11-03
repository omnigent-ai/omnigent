import { useState, type ReactNode } from "react";
import { CheckIcon, CopyIcon } from "lucide-react";

import { Button } from "@/components/ui/button";
import { showToast } from "@/components/ui/toast";
import { copyText } from "@/lib/clipboard";

/** Numbered marker for a step in a sign-in dialog. */
export function StepNumber({ children }: { children: ReactNode }) {
  return (
    <span className="flex size-5 shrink-0 items-center justify-center rounded-full border border-border bg-muted text-xs">
      {children}
    </span>
  );
}

/** Copies a sign-in link or code, falling back to a toast when the clipboard is blocked. */
export function CopyButton({
  text,
  label,
  iconOnly = false,
}: {
  text: string;
  label: string;
  iconOnly?: boolean;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      type="button"
      variant="outline"
      size={iconOnly ? "icon-sm" : "sm"}
      aria-label={label}
      onClick={() => {
        void copyText(text)
          .then(() => setCopied(true))
          .catch(() => showToast("Couldn't copy — select and copy manually.", { duration: 0 }));
      }}
    >
      {copied ? <CheckIcon className="size-3.5" /> : <CopyIcon className="size-3.5" />}
      {!iconOnly && (copied ? "Copied" : "Copy link")}
    </Button>
  );
}
