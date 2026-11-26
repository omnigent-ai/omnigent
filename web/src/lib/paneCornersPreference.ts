// Per-device choice between floating panes and sharp corners. Automatic follows the server's
// `floating_panes` policy live; Floating and Sharp corners override it on this device.

import { createStoredChoicePreference } from "./storedChoicePreference";

export const PANE_CORNERS_STORAGE_KEY = "omnigent:pane-corners";

export const PANE_CORNERS_CHOICES = ["automatic", "floating", "sharp"] as const;

export type PaneCornersChoice = (typeof PANE_CORNERS_CHOICES)[number];

export const PANE_CORNERS_LABELS: Record<PaneCornersChoice, string> = {
  automatic: "Automatic",
  floating: "Floating panes",
  sharp: "Sharp corners",
};

export const paneCornersPreference = createStoredChoicePreference<PaneCornersChoice>(
  PANE_CORNERS_STORAGE_KEY,
  PANE_CORNERS_CHOICES,
  "automatic",
);

export function isPaneCornersChoice(value: string): value is PaneCornersChoice {
  return (PANE_CORNERS_CHOICES as readonly string[]).includes(value);
}

/**
 * Whether the panes float, from the user's choice and the server's policy. A manual choice
 * applies only where the floating design exists, which is with `workspace_sidebar_v2` or the policy on.
 */
export function resolveFloatingPanes({
  choice,
  policyFloating,
  designAvailable,
}: {
  choice: PaneCornersChoice;
  policyFloating: boolean;
  designAvailable: boolean;
}): boolean {
  if (!designAvailable) return false;
  if (choice === "floating") return true;
  if (choice === "sharp") return false;
  return policyFloating;
}
