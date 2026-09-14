import type { KeySequence } from "./types";

/** Escape is a reserved dismissal key, not a user-customizable command binding. */
export function isReservedEscapeSequence(sequence: KeySequence): boolean {
  return sequence.some(
    (stroke) =>
      (stroke.key.kind === "key" && stroke.key.value === "Escape") ||
      (stroke.key.kind === "code" && stroke.key.value === "Escape"),
  );
}
