import type { SVGProps } from "react";

// Official pi.dev logo mark, path data verbatim from https://pi.dev/logo-auto.svg.
// The asset picks black/white via a prefers-color-scheme media query; we use
// currentColor instead so the glyph follows the app theme like its siblings.
// The viewBox pads the mark's 165.29–634.72 bounds by ~5% per side so it sits
// at the same optical size as the lucide/lobehub icons next to it.
export function PiIcon(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="141.82 141.82 516.37 516.37" fill="currentColor" aria-hidden="true" {...props}>
      {/* P shape: outer boundary clockwise, inner hole counter-clockwise */}
      <path
        fillRule="evenodd"
        d="M165.29 165.29 H517.36 V400 H400 V517.36 H282.65 V634.72 H165.29 Z M282.65 282.65 V400 H400 V282.65 Z"
      />
      {/* i dot */}
      <path d="M517.36 400 H634.72 V634.72 H517.36 Z" />
    </svg>
  );
}
