import { settingsEntry } from "./types";

export const POLICIES_ADD_ENTRY = settingsEntry({
  id: "policies-add",
  section: "policies",
  label: "Add policy",
  description: "Apply one of the server's policies to every session, with its parameters.",
  keywords: ["policy", "global policy", "guardrail", "rule", "add", "all sessions", "admin"],
  parts: {
    add: "Add policy",
    filter: "Filter policies",
    change: "Change",
    name: "Policy name",
    parameterValues: "Parameter values",
    submit: "Add",
  },
  // A list parameter's values, named after the parameter, such as "Parameter values: pii_types".
  nameTemplates: { parameterValues: { before: "Parameter values: " } },
  // The server's policies to choose from, then the chosen one's name and parameters.
  presence: {
    filter: { overlay: "add", states: ["choosing"] },
    change: { overlay: "add", states: ["configuring"] },
    name: { overlay: "add", states: ["configuring"] },
    parameterValues: { overlay: "add", states: ["list parameter"] },
    submit: { overlay: "add" },
  },
  overlays: { add: { dialogActions: ["Cancel"] } },
  // The policies and parameters come from the server; none of their names are searchable.
  regions: {
    registry: { rowActions: { choose: { dataName: true } } },
    parameters: { rowFields: { value: { before: "Parameter " } } },
    values: { rowActions: { remove: { dataName: true } } },
  },
  focus: "add",
});

export const POLICIES_LIST_ENTRY = settingsEntry({
  id: "policies-list",
  section: "policies",
  label: "Global policies",
  description: "The policies applied to every session, and whether each one is on.",
  keywords: ["policies", "guardrails", "rules", "enable", "disable", "remove", "all sessions"],
  parts: { refresh: "Refresh" },
  // Each policy's own switch and Remove, and the confirmation Remove opens.
  regions: {
    policies: {
      rowActions: { toggle: { dataName: true }, remove: "Remove policy" },
      rowOverlays: { remove: { rowActions: { cancel: "Cancel", remove: "Remove" } } },
    },
  },
});

export const POLICIES_SETTINGS = [POLICIES_ADD_ENTRY, POLICIES_LIST_ENTRY] as const;
