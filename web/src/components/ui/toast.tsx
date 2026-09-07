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
