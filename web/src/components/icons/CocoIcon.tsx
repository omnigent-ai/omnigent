import type { SVGProps } from "react";

// Snowflake CoCo (Cortex Code) glyph — an original six-armed snowflake with
// chevron branches, evoking the product's Snowflake home without copying the
// vendor's brand asset. Drawn in currentColor so it follows the app theme
// like its sibling icons; one arm (spoke + branch pair) is authored once and
// stamped at 60° rotations around the center for exact symmetry.
export function CocoIcon(props: SVGProps<SVGSVGElement>) {
  // One arm pointing up from the center: the spoke plus a chevron branch pair.
  const arm = "M12 12 V 4.6 M12 7.4 L 9.9 5.9 M12 7.4 L 14.1 5.9";
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {[0, 60, 120, 180, 240, 300].map((angle) => (
        <path key={angle} d={arm} transform={`rotate(${angle} 12 12)`} />
      ))}
    </svg>
  );
}
