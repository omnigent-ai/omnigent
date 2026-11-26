import { PROACTIVE_AGENT_DISPLAY_NAME, PROACTIVE_AGENT_SCOPE_COPY } from "@/lib/proactiveInbox";

import { settingsEntry } from "./types";

/** The workspace's Omni Advisor switch, which only an admin sees. */
export const PROACTIVE_AGENT_WORKSPACE_ENTRY = settingsEntry({
  id: "proactive-agent-workspace",
  section: "admin",
  label: PROACTIVE_AGENT_DISPLAY_NAME,
  description: PROACTIVE_AGENT_SCOPE_COPY.workspace,
  keywords: ["omni advisor", "proactive", "workspace", "admin", "inbox"],
  parts: {
    retry: "Retry",
    prompt: "Prompt",
    save: "Save prompt",
    reset: "Restore default",
    users: "Users to run now",
    run: "Run now",
  },
  presence: {
    // The retry button shows only after the settings fail to load.
    retry: { states: ["load failed"] },
    // The admin controls show once the settings have loaded or failed; the editor, once the
    // prompt has loaded, as its own read.
    prompt: { states: ["prompt loaded"] },
    save: { states: ["prompt loaded"] },
    reset: { states: ["prompt loaded"] },
    users: { states: ["settings loaded", "load failed"] },
    run: { states: ["settings loaded", "load failed"] },
  },
  available: (availability) => availability.proactiveAgent,
});

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

export const ADMIN_SETTINGS = [
  PROACTIVE_AGENT_WORKSPACE_ENTRY,
  NESTED_AGENT_DESTINATIONS_ENTRY,
] as const;
