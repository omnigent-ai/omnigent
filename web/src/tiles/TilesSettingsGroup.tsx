// Settings → General → Tiles: per-device Tile view preferences.

import { SettingsGroup } from "@/components/SettingsGroup";
import { Switch } from "@/components/ui/switch";
import {
  TILES_OPEN_ON_LAUNCH_ENTRY,
  TILES_SHOW_IN_SIDEBAR_ENTRY,
} from "@/pages/settings/search/catalog/general";
import { SettingsEntryLabel, SettingsItem } from "@/pages/settings/search/SettingsItem";
import { settingsControlProps } from "@/pages/settings/search/settingsControl";
import {
  useOpenTilesOnLaunch,
  useShowTilesInSidebar,
  writeOpenTilesOnLaunch,
  writeShowTilesInSidebar,
} from "./tilesPreferences";

export function TilesSettingsGroup() {
  const showInSidebar = useShowTilesInSidebar();
  const openOnLaunch = useOpenTilesOnLaunch();
  return (
    <SettingsGroup title="Tiles" testId="settings-group-tiles">
      <SettingsItem
        entry={TILES_SHOW_IN_SIDEBAR_ENTRY}
        className="flex items-start justify-between gap-6"
      >
        <SettingsEntryLabel entry={TILES_SHOW_IN_SIDEBAR_ENTRY} className="flex-1" />
        <Switch
          {...settingsControlProps(TILES_SHOW_IN_SIDEBAR_ENTRY)}
          checked={showInSidebar}
          onCheckedChange={writeShowTilesInSidebar}
          data-testid="tiles-show-in-sidebar-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.tiles_show_in_sidebar"
        />
      </SettingsItem>
      <SettingsItem
        entry={TILES_OPEN_ON_LAUNCH_ENTRY}
        className="mt-4 flex items-start justify-between gap-6 border-t border-border pt-4"
      >
        <SettingsEntryLabel entry={TILES_OPEN_ON_LAUNCH_ENTRY} className="flex-1" />
        <Switch
          {...settingsControlProps(TILES_OPEN_ON_LAUNCH_ENTRY)}
          checked={openOnLaunch}
          onCheckedChange={writeOpenTilesOnLaunch}
          data-testid="tiles-open-on-launch-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.tiles_open_on_launch"
        />
      </SettingsItem>
    </SettingsGroup>
  );
}
