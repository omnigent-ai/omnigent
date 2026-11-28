// Otto in his tide pool: the illustration behind the factory's small playful touches.
// Decorative only, so it's hidden from assistive tech; the text beside it carries meaning.

import { useId } from "react";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { cn } from "@/lib/utils";

/** Otto resting on a sandbar in soft pink water, with a few bubbles and ripples. */
export function TidePoolScene({
  className,
  ottoClassName,
}: {
  className?: string;
  /** Classes for Otto himself, e.g. `otto-working` for his blink. */
  ottoClassName?: string;
}) {
  // Unique gradient ids, so two scenes on one page don't share (and lose) their fills.
  const id = useId().replace(/:/g, "");
  return (
    <svg
      viewBox="0 0 260 170"
      aria-hidden="true"
      className={cn("h-auto w-52 overflow-visible", className)}
      data-testid="tide-pool-scene"
    >
      <defs>
        <radialGradient id={`${id}-water`} cx="50%" cy="60%" r="50%">
          <stop offset="0" stopColor="#f472b6" stopOpacity="0.22" />
          <stop offset="0.7" stopColor="#f472b6" stopOpacity="0.08" />
          <stop offset="1" stopColor="#f472b6" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={`${id}-sand`} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor="#fde7c8" />
          <stop offset="1" stopColor="#f9d9ae" />
        </linearGradient>
      </defs>
      <ellipse cx="130" cy="96" rx="128" ry="74" fill={`url(#${id}-water)`} />
      <path
        d="M40 140 Q130 112 220 140 Q130 158 40 140Z"
        fill={`url(#${id}-sand)`}
        opacity="0.75"
      />
      <g fill="none" stroke="#f472b6" strokeLinecap="round" strokeWidth="2">
        <path d="M30 70 q15 -8 30 0 t30 0" opacity="0.5" />
        <path d="M178 54 q12 -7 24 0 t24 0" opacity="0.4" />
      </g>
      <g fill="none" stroke="#f472b6" strokeWidth="1.5">
        <circle cx="186" cy="92" r="5" opacity="0.6" />
        <circle cx="198" cy="76" r="3" opacity="0.5" />
        <circle cx="72" cy="100" r="3.5" opacity="0.5" />
      </g>
      <OttoIcon x="88" y="58" width="84" height="84" className={ottoClassName} />
    </svg>
  );
}

/** A small "Done" mark for a finished ticket: Otto beside the word, with one gentle wiggle. */
export function OttoDoneMark() {
  return (
    <span
      className="inline-flex shrink-0 items-center gap-1 rounded-full bg-gradient-to-r from-pink-100 to-orange-50 py-0.5 pr-2 pl-0.5 text-xs font-semibold text-pink-700 dark:from-pink-500/20 dark:to-orange-400/10 dark:text-pink-300"
      title="A ticket you asked for is done"
      data-testid="otto-done-mark"
    >
      <OttoIcon className="otto-wiggle size-5" />
      Done
    </span>
  );
}
