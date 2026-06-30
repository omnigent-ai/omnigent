import { useEffect, useRef } from "react";
import { OttoIcon } from "@/components/icons/OttoIcon";

// Eye geometry in the SVG's own viewBox coordinate system (0 0 1024 1024).
// Each of Otto's eyes is a fixed near-circular white with a concentric pupil
// drawn on top; the pupil can slide until its rim meets the inner edge of
// the white, i.e. up to (whiteRadius - pupilRadius) away from the eye center.
const VIEWBOX_W = 1024;
const VIEWBOX_H = 1024;
// Radii measured off the main-eye paths in OttoIcon.tsx.
const WHITE_RADIUS = 71.3;
const PUPIL_RADIUS = 55.9;
// How far a pupil may travel before its edge touches the white rim. Capped
// well below that geometric max (~15.4) to keep the same travel-to-eye-size
// ratio (~13% of the white radius) as the previous mascots.
const MAX_OFFSET = Math.min(9.3, WHITE_RADIUS - PUPIL_RADIUS);

// Centers of Otto's two eyes in `g.otto-pupil` document order — right eye
// first, then left, matching OttoIcon.tsx. The buddy starfish has no pupil
// groups, so its eyes stay still.
const EYE_CENTERS = [
  { cx: 619.1, cy: 520.6 },
  { cx: 413.8, cy: 520.6 },
];

/**
 * The Omnigent starfish mascot (Otto) with eyes that track the cursor: each
 * black pupil slides to the inner edge of its white eye on the side nearest
 * the pointer.
 *
 * The art lives in OttoIcon; this component drives its two `g.otto-pupil`
 * groups (black disc + glint) through the forwarded ref. Updates are
 * coalesced into a single rAF callback and applied straight to the DOM
 * nodes, so tracking the cursor never re-renders React. Respects
 * `prefers-reduced-motion` by leaving the pupils centered.
 */
export function OttoEyes({ className }: { className?: string }) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;

    // Class-selector contract with OttoIcon (pinned by OttoIcon.test.tsx);
    // querySelectorAll fails silently, so a rename would freeze the eyes.
    const pupils = Array.from(svg.querySelectorAll<SVGGElement>("g.otto-pupil"));
    for (const pupil of pupils) {
      // Smooths each pupil's slide toward its target rather than snapping.
      pupil.style.transition = "transform 90ms ease-out";
      pupil.style.willChange = "transform";
    }

    let frame = 0;
    let pointer: { x: number; y: number } | null = null;

    const apply = () => {
      frame = 0;
      if (!pointer) return;
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      EYE_CENTERS.forEach((eye, i) => {
        const pupil = pupils[i];
        if (!pupil) return;
        // Eye center in screen space. The viewBox maps uniformly into the
        // rendered box (matching aspect ratio, default preserveAspectRatio),
        // so a single scale per axis is exact.
        const eyeX = rect.left + (eye.cx / VIEWBOX_W) * rect.width;
        const eyeY = rect.top + (eye.cy / VIEWBOX_H) * rect.height;
        const dx = pointer!.x - eyeX;
        const dy = pointer!.y - eyeY;
        const dist = Math.hypot(dx, dy);
        if (dist < 0.0001) {
          pupil.style.transform = "translate(0px, 0px)";
          return;
        }
        // Always ride the rim toward the cursor. translate() px units on an
        // SVG element resolve to user-space units, so MAX_OFFSET is correct.
        const tx = (dx / dist) * MAX_OFFSET;
        const ty = (dy / dist) * MAX_OFFSET;
        pupil.style.transform = `translate(${tx.toFixed(3)}px, ${ty.toFixed(3)}px)`;
      });
    };

    const onMove = (e: PointerEvent) => {
      pointer = { x: e.clientX, y: e.clientY };
      if (!frame) frame = requestAnimationFrame(apply);
    };

    window.addEventListener("pointermove", onMove, { passive: true });
    return () => {
      window.removeEventListener("pointermove", onMove);
      if (frame) cancelAnimationFrame(frame);
    };
  }, []);

  return (
    <OttoIcon
      ref={svgRef}
      className={className}
      // The hero mascot is meaningful (not decorative), so OttoIcon's
      // decorative aria-hidden default is overridden with image semantics.
      role="img"
      aria-label="Omnigent"
      aria-hidden={false}
    />
  );
}
