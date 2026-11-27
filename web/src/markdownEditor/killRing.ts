// One kill ring for the pull request page's editors, so ⌃Y in a reply pastes what ⌃W removed in
// the description. The chat composer keeps its own ring per editor, like a terminal prompt.

import {
  EMPTY_KILL_RING,
  endKillRun,
  type KillRing,
  pushKill,
  yankText,
} from "@/lib/textEditing/readline";

/** A kill ring editors share: kills join while they run back to back, and ⌃Y yanks the newest. */
export interface SharedKillRing {
  push: (text: string, direction: "backward" | "forward") => void;
  /** Anything that isn't a kill, so the next kill starts its own entry. */
  breakSequence: () => void;
  /** The newest kill, for ⌃Y; empty when nothing was killed. */
  yank: () => string;
}

export function createSharedKillRing(): SharedKillRing {
  let ring: KillRing = EMPTY_KILL_RING;
  return {
    push: (text, direction) => {
      if (text) ring = pushKill(ring, text, direction === "backward");
    },
    breakSequence: () => {
      ring = endKillRun(ring);
    },
    yank: () => {
      ring = endKillRun(ring);
      return yankText(ring) ?? "";
    },
  };
}

export const sharedKillRing = createSharedKillRing();
