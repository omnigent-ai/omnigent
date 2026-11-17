// ⌘K's way into Settings search: it opens Settings where the user is, or where Settings opens by
// default, and hands focus to the search field.

import { SearchIcon } from "lucide-react";

import { type SettingsSectionId, settingsSectionPath } from "@/shell/settingsNav";
import type { ActionCommand } from "@/shell/appCommands";

import { requestSettingsSearchFocus } from "./settingsSearchFocus";

export const SEARCH_SETTINGS_LABEL = "Search settings…";

/**
 * Where the command goes. Inside Settings it stays on the current section, without the URL's row,
 * so a pending row reveal can't take focus back from the field. Outside, phones open the Settings
 * menu and larger screens the default section.
 */
export function settingsSearchPath({
  currentSection,
  mobile,
}: {
  currentSection: SettingsSectionId | null;
  mobile: boolean;
}): string {
  if (currentSection !== null) return settingsSectionPath(currentSection);
  return mobile ? "/settings" : settingsSectionPath("general");
}

export function settingsSearchCommand(options: {
  currentSection: SettingsSectionId | null;
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
