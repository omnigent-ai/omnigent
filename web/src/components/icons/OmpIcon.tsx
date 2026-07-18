import type { SVGProps } from "react";

// omp (Oh My Pi) mark: a stylized omega (ω) — a nod to "Oh My" — kept visually
// distinct from the Pi glyph. Uses currentColor so it follows the app theme
// like its sibling brand icons.
export function OmpIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true" {...props}>
      <path
        d="M4 19c0-1.6 1-2.8 2.3-4.2C7.8 13.2 8.8 11.9 8.8 10a3.2 3.2 0 0 0-6.4 0M20 19c0-1.6-1-2.8-2.3-4.2-1.5-1.6-2.5-2.9-2.5-4.8a3.2 3.2 0 0 1 6.4 0"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M8.5 12a3.5 3.5 0 0 0 7 0"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path d="M2 19h4M18 19h4" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
    </svg>
  );
}
