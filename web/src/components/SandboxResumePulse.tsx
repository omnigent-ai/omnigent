import type { HTMLAttributes } from "react";

import { cn } from "@/lib/utils";

/**
 * The resuming sandbox's breathing dot, shared by the composer's host dot and
 * every workspace notice: one beat every two seconds, and a still halo when
 * motion is reduced. Theme tokens keep it legible in light and dark.
 */
export function SandboxResumePulse({
  size = "md",
  className,
  ...props
}: { size?: "sm" | "md" } & HTMLAttributes<HTMLSpanElement>) {
  const dot = size === "sm" ? "size-2" : "size-2.5";
  return (
    <span
      aria-hidden
      data-testid="sandbox-resume-pulse"
      {...props}
      className={cn("relative inline-flex shrink-0", dot, className)}
    >
      <span className="absolute inset-0 rounded-full bg-primary/40 animate-[sandbox-resume-breathe_2s_ease-in-out_infinite] motion-reduce:-inset-1 motion-reduce:animate-none" />
      <span className={cn("relative inline-flex rounded-full bg-primary", dot)} />
    </span>
  );
}
