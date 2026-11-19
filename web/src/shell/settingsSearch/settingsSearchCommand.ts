// ⌘K's way into Settings search: it opens Settings where the user is, or where Settings opens by
// default, and hands focus to the search field.

import { SearchIcon } from "lucide-react";

import { settingsSectionPath } from "@/shell/settingsNav";
import type { ActionCommand } from "@/shell/appCommands";

import { requestSettingsSearchFocus } from "./settingsSearchFocus";

export const SEARCH_SETTINGS_LABEL = "Search settings…";

/**
 * Where the command goes. Inside Settings it stays on the current page, query included, so an
 * open editor keeps its draft; only the URL's row hash goes, so a pending row reveal can't take
 * focus back from the field. Outside, phones open the Settings menu and larger screens the
 * default section.
 */
export function settingsSearchPath({
  settingsLocation,
  mobile,
}: {
  /** The current Settings page, or null outside Settings. */
  settingsLocation: { pathname: string; search: string } | null;
  mobile: boolean;
}): string {
  if (settingsLocation !== null) return `${settingsLocation.pathname}${settingsLocation.search}`;
  return mobile ? "/settings" : settingsSectionPath("general");
}

export function settingsSearchCommand(options: {
  settingsLocation: { pathname: string; search: string } | null;
  mobile: boolean;
  navigate: (path: string) => void;
}): ActionCommand {
  return {
    id: "settings-search",
    label: SEARCH_SETTINGS_LABEL,
    icon: SearchIcon,
    keywords: ["settings", "preferences", "options", "find", "search"],
    run: () => {
      requestSettingsSearchFocus();
      options.navigate(settingsSearchPath(options));
    },
  };
}
