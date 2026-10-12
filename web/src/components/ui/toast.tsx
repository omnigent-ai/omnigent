import type { ReactNode } from "react";
import { toast } from "sonner";

/**
 * Compatibility wrapper for existing call sites while they migrate to Sonner.
 * The window event remains for lightweight consumers and legacy tests that
 * observe toast activity without mounting the renderer.
 */
export function showToast(content: ReactNode, opts?: { duration?: number }): string | number {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new CustomEvent("omnigent:toast", { detail: { content } }));
  }
  return toast(content, {
    closeButton: true,
    duration:
      opts?.duration !== undefined && opts.duration <= 0
        ? Number.POSITIVE_INFINITY
        : opts?.duration,
    testId: "toast",
  });
}

/**
 * Dismiss every user-dismissible toast. Sonner's bare `toast.dismiss()` also
 * removes `dismissible: false` cards such as the terminal clipboard prompt,
 * whose owners only re-publish them when their props change, so a pending
 * decision would vanish with no way back. Those cards stay put.
 */
export function dismissAllToasts(): void {
  for (const active of toast.getToasts()) {
    if ("dismissible" in active && active.dismissible === false) continue;
    toast.dismiss(active.id);
  }
}
