// Per-device Tile view preferences: the last layout, whether Tiles shows in the
// sidebar, and whether the app opens in Tiles.

import { createStoredChoicePreference } from "@/lib/storedChoicePreference";
import { TILES_VIEWS, type TilesView } from "./tileSelection";

const viewPreference = createStoredChoicePreference("omnigent:tiles-view", TILES_VIEWS, "gallery");
const showInSidebarPreference = createStoredChoicePreference(
  "omnigent:tiles-show-in-sidebar",
  ["true", "false"],
  "true",
);
const openOnLaunchPreference = createStoredChoicePreference(
  "omnigent:tiles-open-on-launch",
  ["false", "true"],
  "false",
);

export const readTilesView = (): TilesView => viewPreference.read();
export const writeTilesView = (view: TilesView): void => viewPreference.write(view);

export const useShowTilesInSidebar = (): boolean => showInSidebarPreference.use() === "true";
export const writeShowTilesInSidebar = (enabled: boolean): void =>
  showInSidebarPreference.write(enabled ? "true" : "false");

export const readOpenTilesOnLaunch = (): boolean => openOnLaunchPreference.read() === "true";
export const useOpenTilesOnLaunch = (): boolean => openOnLaunchPreference.use() === "true";
export const writeOpenTilesOnLaunch = (enabled: boolean): void =>
  openOnLaunchPreference.write(enabled ? "true" : "false");
