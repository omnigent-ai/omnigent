import { settingsEntry } from "./types";

export const NESTED_AGENT_DESTINATIONS_ENTRY = settingsEntry({
  id: "nested-agent-destinations",
  section: "admin",
  label: "Nested agent destinations",
  description:
    "Where agents added to a session can run: only its host, its host or a new sandbox, or any available host.",
  keywords: ["nested agents", "add agent", "sibling sessions", "hosts", "sandbox", "server"],
  choices: ["Current host only", "Current host or new sandbox", "Any available host"],
  overlays: { "": {} },
});

export const ADMIN_SETTINGS = [NESTED_AGENT_DESTINATIONS_ENTRY] as const;
