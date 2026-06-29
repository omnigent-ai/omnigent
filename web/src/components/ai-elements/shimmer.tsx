"use client";

import { cn } from "@/lib/utils";
import type { CSSProperties, ElementType } from "react";
import { createElement, memo, useMemo } from "react";

export interface TextShimmerProps {
  children: string;
  as?: ElementType;
  className?: string;
  duration?: number;
  spread?: number;
}

// Pure-CSS text shimmer (see `.text-shimmer` / `@keyframes shimmer-sweep` in
// index.css). Previously driven by framer-motion; the animation is a simple
// infinite background-position sweep, so CSS does it without the ~165KB lib.
const ShimmerComponent = ({
  children,
  as: Component = "p",
  className,
  duration = 2,
  spread = 2,
}: TextShimmerProps) => {
  const dynamicSpread = useMemo(() => (children?.length ?? 0) * spread, [children, spread]);

  return createElement(
    Component,
    {
      // `text-shimmer` is applied outside cn()/tailwind-merge: twMerge parses it
      // as a text-color utility and drops it as conflicting with text-transparent,
      // which would silently kill the animation. The class drives the keyframes;
      // it carries no color of its own, so it can't actually conflict.
      className: `text-shimmer ${cn(
        "relative inline-block bg-[length:250%_100%,auto] bg-clip-text text-transparent",
        "[--bg:linear-gradient(90deg,#0000_calc(50%-var(--spread)),var(--color-background),#0000_calc(50%+var(--spread)))] [background-repeat:no-repeat,padding-box]",
        className,
      )}`,
      style: {
        "--spread": `${dynamicSpread}px`,
        "--shimmer-duration": `${duration}s`,
        backgroundImage:
          "var(--bg), linear-gradient(var(--color-muted-foreground), var(--color-muted-foreground))",
      } as CSSProperties,
    },
    children,
  );
};

export const Shimmer = memo(ShimmerComponent);
