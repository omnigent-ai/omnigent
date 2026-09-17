/**
 * Build the Devin Fusion selector sections (Lead / Effort / Sidekick, with Fast
 * and Priority checkboxes) for the shared {@link ComposerConfigSections} menu.
 *
 * Shared by both harness pickers — the landing dialog and the in-session
 * composer — so they render the same Fusion controls. Each control resolves the
 * change back to a real combo id via {@link applyFusionChange}, so the menu only
 * ever produces a launchable Fusion variant.
 */
import { normalizeEffortLabel } from "@/lib/composerModelLabel";
import {
  applyFusionChange,
  currentFusionCombo,
  fusionEffortChoices,
  fusionFastAvailable,
  fusionLeadChoices,
  fusionPriorityAvailable,
  fusionSidekickChoices,
} from "@/lib/devinFusion";
import type { FusionDescriptor } from "@/lib/types";
import type { ComposerConfigSection } from "./ComposerConfigSections";

export function buildFusionSections({
  descriptor,
  modelUid,
  testIdPrefix,
  onChange,
  disabled = false,
}: {
  descriptor: FusionDescriptor;
  /** The currently selected Fusion combo id (or the bare `fusion` family). */
  modelUid: string;
  /** e.g. `new-chat-landing-agent` or `composer-agent`. */
  testIdPrefix: string;
  /** Called with the resolved combo id when any facet changes. */
  onChange: (modelUid: string) => void;
  disabled?: boolean;
}): ComposerConfigSection[] {
  const current = currentFusionCombo(descriptor, modelUid);
  const change = (facet: Parameters<typeof applyFusionChange>[2]) => () =>
    onChange(applyFusionChange(descriptor, modelUid, facet));

  const lead: ComposerConfigSection = {
    testId: `${testIdPrefix}-fusion-leads`,
    header: "Lead",
    choices: fusionLeadChoices(descriptor).map(({ value, label }) => ({
      key: value,
      label,
      checked: current.lead === value,
      disabled,
      onSelect: change({ lead: value }),
      testId: `${testIdPrefix}-fusion-lead-${value}`,
    })),
  };

  const effortChoices = fusionEffortChoices(descriptor, current.lead).map((rung) => ({
    key: rung,
    label: normalizeEffortLabel(rung),
    checked: current.effort === rung,
    disabled,
    onSelect: change({ effort: rung }),
    testId: `${testIdPrefix}-fusion-effort-${rung}`,
  }));
  if (fusionFastAvailable(descriptor, current.lead, current.effort)) {
    effortChoices.push({
      key: "__fast__",
      label: "Fast",
      checked: current.fast,
      disabled,
      onSelect: change({ fast: !current.fast }),
      testId: `${testIdPrefix}-fusion-fast`,
    });
  }
  const effort: ComposerConfigSection = {
    testId: `${testIdPrefix}-fusion-efforts`,
    header: "Effort",
    choices: effortChoices,
  };

  const sidekickChoices = fusionSidekickChoices(
    descriptor,
    current.lead,
    current.effort,
    current.fast,
  ).map(({ value, label }) => ({
    key: value,
    label,
    checked: current.sidekick === value,
    disabled,
    onSelect: change({ sidekick: value }),
    testId: `${testIdPrefix}-fusion-sidekick-${value}`,
  }));
  if (
    fusionPriorityAvailable(
      descriptor,
      current.lead,
      current.effort,
      current.fast,
      current.sidekick,
    )
  ) {
    sidekickChoices.push({
      key: "__priority__",
      label: "Priority",
      checked: current.priority,
      disabled,
      onSelect: change({ priority: !current.priority }),
      testId: `${testIdPrefix}-fusion-priority`,
    });
  }
  const sidekick: ComposerConfigSection = {
    testId: `${testIdPrefix}-fusion-sidekicks`,
    header: "Sidekick",
    choices: sidekickChoices,
  };

  return [lead, effort, sidekick];
}
