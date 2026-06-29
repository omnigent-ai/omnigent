// Regression tests for the Button pressed-state nudge vs. translate-based
// positioning. Call sites center absolutely-positioned buttons with
// `top-1/2 -translate-y-1/2` (sidebar quick-pin, carousel arrows). In
// Tailwind v4 every `translate-*` utility writes the same `translate` CSS
// property, so a stateful utility like `active:translate-y-px` in the Button
// base REPLACES the -50% centering while pressed — the button jumps half a
// row out from under the cursor and the click lands elsewhere. The press
// feedback must therefore use the separate `transform` property, which
// composes with `translate` instead of overriding it.
//
// jsdom can't compute Tailwind styles, so geometry isn't directly testable;
// these tests pin the class-level invariant that produced the bug.

import { describe, expect, it } from "vitest";

import { buttonVariants } from "./button";

// Matches a Tailwind translate utility (`translate-y-px`, `-translate-x-1/2`,
// `translate-3`), bare or behind variant prefixes (`active:translate-y-px`).
// Does NOT match the arbitrary-property press nudge `[transform:translateY(1px)]`
// (no hyphen after "translate").
const TRANSLATE_UTILITY = /(^|:)-?translate-/;

const VARIANTS = ["default", "outline", "secondary", "ghost", "destructive", "link"] as const;
const SIZES = ["default", "xs", "sm", "lg", "icon", "icon-xs", "icon-sm", "icon-lg"] as const;

describe("buttonVariants translate/transform composition", () => {
  it.each(VARIANTS.flatMap((variant) => SIZES.map((size) => ({ variant, size }))))(
    "emits no translate-* utility for variant=$variant size=$size",
    ({ variant, size }) => {
      const classes = buttonVariants({ variant, size }).split(/\s+/);
      // A translate utility here (e.g. reintroducing active:translate-y-px)
      // would clobber caller positioning transforms on press: the sidebar
      // quick-pin button jumps mid-click and the click never registers.
      const offenders = classes.filter((c) => TRANSLATE_UTILITY.test(c));
      expect(offenders).toEqual([]);
    },
  );

  it("keeps the pressed-state nudge on the transform property", () => {
    // The press feedback must exist and must be an arbitrary `transform:`
    // property under the active: variant. If this fails, either the press
    // feedback was removed (intentional? update this test) or it was moved
    // back to a translate utility (reintroduces the jumping-pin bug).
    expect(buttonVariants({})).toMatch(/active:[^\s]*\[transform:translateY\(/);
  });

  it("preserves a caller's -translate-y-1/2 centering class through the merge", () => {
    // Guards against tailwind-merge treating the press nudge and the caller's
    // centering class as conflicting and dropping the latter — that would
    // leave the pin button permanently mispositioned, not just while pressed.
    const merged = buttonVariants({
      variant: "ghost",
      size: "icon-sm",
      className: "absolute top-1/2 -translate-y-1/2 right-9",
    });
    expect(merged).toContain("-translate-y-1/2");
  });
});
