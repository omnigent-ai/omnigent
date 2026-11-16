import type { UpdateMode } from "@/lib/nativeBridge";

import { settingsEntry } from "./types";

/** How often the app looks for updates, as the Update mode menu names each choice. */
export const UPDATE_MODE_LABELS: Readonly<Record<UpdateMode, string>> = {
  default: "Automatic (check periodically, ask before installing)",
  start: "Check when Omnigent starts",
  manual: "Manual only",
  none: "Off",
};

export const UPDATE_MODE_ENTRY = settingsEntry({
  id: "update-mode",
  section: "updates",
  label: "Update mode",
  description: "When the app looks for a new version, and whether it asks before installing it.",
  keywords: ["update", "upgrade", "new version", "check", "automatic", "manual", "off"],
  choices: Object.values(UPDATE_MODE_LABELS),
});

export const INSTALL_UPDATES_ON_QUIT_ENTRY = settingsEntry({
  id: "install-updates-on-quit",
  section: "updates",
  label: "Install downloaded updates on next quit",
  description: "Applies only after you choose to download an update.",
  keywords: ["update", "install", "quit", "restart", "download", "automatic"],
});

export const CHECK_FOR_UPDATES_ENTRY = settingsEntry({
  id: "check-for-updates",
  section: "updates",
  label: "Check for updates",
  description: "Look for a new version of the app right away.",
  keywords: ["update", "upgrade", "new version", "check", "latest"],
  parts: { check: "Check for updates now" },
  focus: "check",
});

export const UPDATES_SETTINGS = [
  UPDATE_MODE_ENTRY,
  INSTALL_UPDATES_ON_QUIT_ENTRY,
  CHECK_FOR_UPDATES_ENTRY,
] as const;
