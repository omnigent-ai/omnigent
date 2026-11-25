// The matchers Settings search ranks by: a typed chord first, then text, as ⌘K ranks its rows.

import type { ShortcutEnvironment, ShortcutId } from "@/lib/shortcuts/registry";

import { chordMatcher } from "./chordQuery";
import { type SettingsSearchMatcher, textMatcher } from "./settingsSearchIndex";

export function settingsSearchMatchers(
  environment: ShortcutEnvironment,
  listedShortcutRows: ReadonlySet<ShortcutId>,
): readonly SettingsSearchMatcher[] {
  return [chordMatcher(environment, listedShortcutRows), textMatcher];
}
