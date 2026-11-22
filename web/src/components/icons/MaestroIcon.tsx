import type { SVGProps } from "react";
import { cn } from "@/lib/utils";

/** Maestro's conductor avatar, independent of its harness. */
export function MaestroIcon({ className, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 64 64"
      fill="none"
      aria-hidden="true"
      data-agent-icon="maestro"
      className={cn("size-4 shrink-0", className)}
      {...props}
    >
      <path d="m47 31 13-26" stroke="#29364f" strokeWidth="4.5" strokeLinecap="round" />
      <path d="m47 31 13-26" stroke="#f5f8ff" strokeWidth="2.2" strokeLinecap="round" />
      <path
        fill="#7ca5f2"
        d="M28 6c7 0 9 10 13 13s13 0 14 7-7 9-10 14 1 15-5 18-11-5-16-5-12 6-16 1 1-11 0-16-7-8-5-14 11-3 15-8S21 6 28 6Z"
      />
      <g fill="#141219">
        <rect x="20" y="26" width="6.5" height="11.5" rx="3.25" />
        <rect x="33.5" y="26" width="6.5" height="11.5" rx="3.25" />
      </g>
      <path d="M25.7 43q4.3 5.5 8.6 0" stroke="#141219" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}
