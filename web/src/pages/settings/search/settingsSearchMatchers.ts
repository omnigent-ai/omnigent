// The matchers Settings search ranks by: a typed chord first, then text, as ⌘K ranks its rows.

import type { ShortcutEnvironment, ShortcutId } from "@/lib/shortcuts/registry";

import { type ChordSearchScope, chordMatcher } from "./chordQuery";
import { type SettingsSearchMatcher, textMatcher } from "./settingsSearchIndex";

export function settingsSearchMatchers(
  environment: ShortcutEnvironment,
  listedShortcutRows: ReadonlySet<ShortcutId>,
  scope: ChordSearchScope,
): readonly SettingsSearchMatcher[] {
  return [chordMatcher(environment, listedShortcutRows, scope), textMatcher];
}
