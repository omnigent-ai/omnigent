import { OttoIcon } from "@/components/icons/OttoIcon";

/**
 * The Omnigent starfish mascot (Otto) with image semantics for the new-chat
 * hero. The original SVG artwork, including its centered pupils, lives in
 * OttoIcon.
 */
export function OttoEyes({ className }: { className?: string }) {
  return (
    <OttoIcon
      className={className}
      // The hero mascot is meaningful (not decorative), so OttoIcon's
      // decorative aria-hidden default is overridden with image semantics.
      role="img"
      aria-label="Omnigent"
      aria-hidden={false}
    />
  );
}
