// Settings → General → Tiles: per-device Tile view preferences.

import { useId } from "react";
import { SettingsGroup } from "@/components/SettingsGroup";
import { SettingsLabel } from "@/components/SettingsLabel";
import { Switch } from "@/components/ui/switch";
import {
  useOpenTilesOnLaunch,
  useShowTilesInSidebar,
  writeOpenTilesOnLaunch,
  writeShowTilesInSidebar,
} from "./tilesPreferences";

export function TilesSettingsGroup() {
  const showInSidebar = useShowTilesInSidebar();
  const openOnLaunch = useOpenTilesOnLaunch();
  const sidebarLabelId = useId();
  const launchLabelId = useId();
  return (
    <SettingsGroup title="Tiles" testId="settings-group-tiles">
      <div className="flex items-start justify-between gap-6">
        <SettingsLabel
          label="Show Tiles in the sidebar"
          labelId={sidebarLabelId}
          description="Adds a Tiles button that switches between the single agent view and all your active agents as tiles."
        />
        <Switch
          aria-labelledby={sidebarLabelId}
          checked={showInSidebar}
          onCheckedChange={writeShowTilesInSidebar}
          data-testid="tiles-show-in-sidebar-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.tiles_show_in_sidebar"
        />
      </div>
      <div className="mt-4 flex items-start justify-between gap-6 border-t border-border pt-4">
        <SettingsLabel
          label="Open in Tiles"
          labelId={launchLabelId}
          description="Start on the Tile view instead of a new session when the app opens."
        />
        <Switch
          aria-labelledby={launchLabelId}
          checked={openOnLaunch}
          onCheckedChange={writeOpenTilesOnLaunch}
          data-testid="tiles-open-on-launch-toggle"
          className="mt-0.5 shrink-0"
          componentId="settings.general.tiles_open_on_launch"
        />
      </div>
    </SettingsGroup>
  );
}
