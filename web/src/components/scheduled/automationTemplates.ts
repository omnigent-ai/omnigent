import { TRIGGER_SOURCES, TRIGGER_TEMPLATES } from "./triggerTemplates";

// Shared by the full gallery and the empty-state preview.
export const AUTOMATION_TEMPLATES = TRIGGER_SOURCES.map((source) => ({
  ...TRIGGER_TEMPLATES[source],
  id: source,
  trigger: source,
  prefill: { name: TRIGGER_TEMPLATES[source].name, prompt: "" },
  needs: `Requires ${TRIGGER_TEMPLATES[source].toolsExample}.`,
  testId: `suggestion-trigger-${source}`,
}));
