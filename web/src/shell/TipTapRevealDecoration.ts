// Spec for the decoration the user is navigating to (the current find match, the
// active comment), so a node view that hides its content can show it.

import type { DecorationSource } from "@tiptap/pm/view";

export const REVEAL_DECORATION_SPEC = { revealHiddenContent: true } as const;

/**
 * Positions of the decorations in `decorations` carrying REVEAL_DECORATION_SPEC,
 * as a comparable key; "" when there are none.
 */
export function revealDecorationKey(decorations: DecorationSource): string {
  const ranges: string[] = [];
  decorations.forEachSet((set) => {
    const revealing = set.find(undefined, undefined, (spec: { revealHiddenContent?: boolean }) =>
      Boolean(spec.revealHiddenContent),
    );
    for (const decoration of revealing) ranges.push(`${decoration.from}-${decoration.to}`);
  });
  return ranges.join(",");
}
