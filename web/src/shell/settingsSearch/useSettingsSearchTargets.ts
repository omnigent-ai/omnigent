import { useMemo } from "react";

import { useShortcutPageGroups } from "@/components/KeyboardShortcutsDialog";
import { offersAnyHarness, useCuratedOutHarnesses } from "@/hooks/useCuratedOutHarnesses";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import { SETTINGS_ENTRIES } from "@/pages/settings/search/catalog";
import { harnessOfSettingsEntry } from "@/pages/settings/search/catalog/harnesses";
import {
  type SettingsSearchMatcher,
  type SettingsSearchTarget,
  settingsSearchTargets,
} from "@/pages/settings/search/settingsSearchIndex";
import { settingsSearchMatchers } from "@/pages/settings/search/settingsSearchMatchers";
import { shortcutSettingsEntries } from "@/pages/settings/search/shortcutEntries";
import { useSettingsAvailability } from "@/pages/settings/search/useSettingsAvailability";
import { currentShortcutEnvironment, type ShortcutId } from "@/lib/shortcuts/registry";
import type { ShortcutPageRow } from "@/lib/shortcuts/shortcutPage";
import {
  type SettingsNavItem,
  type SettingsSectionId,
  useSettingsNavGroups,
} from "@/shell/settingsNav";

export interface SettingsSearchTargets {
  targets: SettingsSearchTarget[];
  /** The sections that show here, by id, for their icons. */
  sections: ReadonlyMap<SettingsSectionId, SettingsNavItem>;
  /** The live page row behind each shortcut target, for its key caps. */
  shortcutRows: ReadonlyMap<ShortcutId, ShortcutPageRow>;
  /** The matchers to rank by here, a typed chord matched as this environment's hotkeys are. */
  matchers: readonly SettingsSearchMatcher[];
}

/**
 * What Settings search can find here: the catalog's rows and the Keyboard shortcuts page's rows,
 * limited to sections this deploy and device show and rows available to this viewer. A harness
 * the deployment curates out has no rows, as Settings shows it no page.
 */
export function useSettingsSearchTargets(): SettingsSearchTargets {
  const curatedOut = useCuratedOutHarnesses();
  const groups = useSettingsNavGroups(offersAnyHarness(curatedOut));
  const mobile = useIsMobileViewport();
  const availability = useSettingsAvailability();
  const shortcutGroups = useShortcutPageGroups();
  // A string, so the results rebuild only when a field of the environment changes.
  const environment = currentShortcutEnvironment();
  const mac = environment.mac;
  const native = environment.native;
  const desktopMenu = environment.desktopMenu;
  const goMenu = environment.goMenu;
  const submitWithModEnter = environment.submitWithModEnter;
  return useMemo(() => {
    const sections = new Map(
      groups
        .flatMap((group) => group.items)
        .filter((item) => !(mobile && item.hideOnMobile))
        .map((item) => [item.id, item] as const),
    );
    const entries = SETTINGS_ENTRIES.filter((entry) => {
      const harness = harnessOfSettingsEntry(entry);
      return harness === undefined || !curatedOut.has(harness);
    });
    const targets = settingsSearchTargets(
      [...entries, ...shortcutSettingsEntries(shortcutGroups)],
      [...sections.values()].map((item) => ({ id: item.id, label: item.label })),
      availability,
    );
    const shortcutRows = new Map(
      shortcutGroups.flatMap((group) => group.items.map((row) => [row.shortcutId, row] as const)),
    );
    const matchers = settingsSearchMatchers(
      { mac, native, desktopMenu, goMenu, submitWithModEnter },
      new Set(shortcutRows.keys()),
    );
    return { targets, sections, shortcutRows, matchers };
  }, [
    curatedOut,
    groups,
    mobile,
    availability,
    shortcutGroups,
    mac,
    native,
    desktopMenu,
    goMenu,
    submitWithModEnter,
  ]);
}
