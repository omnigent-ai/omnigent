import { settingsEntry } from "./types";

export const LOCAL_CLI_ENTRY = settingsEntry({
  id: "local-cli",
  section: "cli",
  label: "CLI status",
  description:
    "Whether the app found the Omnigent CLI, its version and path, and how to install it or set a custom path.",
  keywords: ["cli", "command line", "omnigent", "path", "binary", "install", "version", "runner"],
  parts: { reset: "Reset to auto-detected" },
  // Only a path set from the connect screen can be reset.
  presence: { reset: { states: ["custom path"] } },
});

export const CLI_SETTINGS = [LOCAL_CLI_ENTRY] as const;
