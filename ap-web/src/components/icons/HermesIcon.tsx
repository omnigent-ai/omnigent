import type { SVGProps } from "react";

// Hermes Agent (Nous Research) glyph — an original caduceus: the winged staff
// with two entwined serpents, the classic emblem of Hermes the messenger.
// Drawn in currentColor so it follows the app theme like its sibling icons;
// authored here (not a third-party brand asset). The left wing + serpent are
// drawn once and the whole set is mirrored around the x=12 center for exact
// symmetry, so the two snakes open the caduceus' twin lens loops.
export function HermesIcon(props: SVGProps<SVGSVGElement>) {
  // One wing (two feathers) spreading from the top of the staff.
  const wing = "M11.5 5.9 C 9 4.7 6.4 4.7 4.8 5.7 " + "M11.5 7.3 C 9.4 6.6 7.2 6.8 5.8 7.9";
  // One serpent weaving down the staff: it bows out, crosses the centre, bows
  // back the other way, and converges at the foot. Mirrored, the two snakes
  // weave the entwined caduceus.
  const snake = "M12 8.2 C 8.9 9 8.6 11 12 12 C 15.4 13 15.1 15 12 16 C 9.9 16.7 9.6 17.6 12 18.2";
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      data-testid="hermes-icon"
      {...props}
    >
      {/* staff */}
      <path d="M12 5 V 20" />
      {/* left wing + serpent */}
      <path d={wing} />
      <path d={snake} />
      {/* right wing + serpent — the left set mirrored around the x=12 center */}
      <g transform="matrix(-1 0 0 1 24 0)">
        <path d={wing} />
        <path d={snake} />
      </g>
    </svg>
  );
}
