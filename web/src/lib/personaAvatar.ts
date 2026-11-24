// Name-derived avatar styling for project personas (identities such as "PM").
// The same name always gets the same color, everywhere it's drawn.

import { stableNameHash } from "./userBadge";

// White text on each fill is at least 4.6:1, and the fills stay saturated
// enough to read against both light and dark surfaces.
const PERSONA_PALETTE = [
  "#1967d2",
  "#137333",
  "#c5221f",
  "#8430ce",
  "#b06000",
  "#00796b",
  "#3f51b5",
  "#ad1457",
] as const;

/** Server-owned session label naming the identity a session works for. */
export const PROJECT_IDENTITY_LABEL_KEY = "omnigent.project_identity_id";

/**
 * One or two initials for a persona's circle: "PM" → "PM", "Designer" → "D",
 * "Release Manager" → "RM", "QA lead" → "QL".
 */
export function personaInitials(name: string): string {
  const words = name
    .trim()
    .split(/[\s._\-/]+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length > 1) return (words[0]![0]! + words[1]![0]!).toUpperCase();
  const word = words[0]!;
  // A short acronym such as "PM" or "QA" keeps both letters.
  if (word.length <= 3 && word === word.toUpperCase() && /[A-Z]/.test(word))
    return word.slice(0, 2);
  return word[0]!.toUpperCase();
}

/** The persona's fill color, chosen by its case-insensitive name. */
export function personaColor(name: string): string {
  return PERSONA_PALETTE[stableNameHash(name.trim().toLowerCase()) % PERSONA_PALETTE.length]!;
}
