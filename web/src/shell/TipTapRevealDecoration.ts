// Spec for the decoration the user is navigating to (the current find match, the
// active comment), so a node view that hides its content can show it.

import type { DecorationSource } from "@tiptap/pm/view";

export const REVEAL_DECORATION_SPEC = { revealHiddenContent: true } as const;

/** True when `decorations` hold a decoration carrying REVEAL_DECORATION_SPEC. */
export function hasRevealDecoration(decorations: DecorationSource): boolean {
  let found = false;
  decorations.forEachSet((set) => {
    found ||=
      set.find(undefined, undefined, (spec: { revealHiddenContent?: boolean }) =>
        Boolean(spec.revealHiddenContent),
      ).length > 0;
  });
  return found;
}
