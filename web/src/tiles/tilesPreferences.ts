// Per-device Tile view preferences: the last layout, whether Tiles shows in the
// sidebar, and whether the app opens in Tiles.

import { createStoredChoicePreference } from "@/lib/storedChoicePreference";
import { TILE_STATUS_FILTERS, type TileStatusFilter } from "./tileFilters";
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

const statusFilterPreference = createStoredChoicePreference(
  "omnigent:tiles-status-filter",
  TILE_STATUS_FILTERS,
  "all",
);
export const useTileStatusFilter = (): TileStatusFilter => statusFilterPreference.use();
export const writeTileStatusFilter = (filter: TileStatusFilter): void =>
  statusFilterPreference.write(filter);
