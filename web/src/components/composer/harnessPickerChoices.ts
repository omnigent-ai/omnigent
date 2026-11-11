// Which rows the new-chat harness picker lists, decided outside the menu: which entries
// show, in what groups and order, given the host and the viewer's Appearance settings.

import type { AvailableAgent } from "@/hooks/useAvailableAgents";
import type { Host } from "@/hooks/useHosts";
import { harnessReadinessOnHost } from "@/lib/harnessSetup";
import { nativeCodingAgentForAvailableAgent } from "@/lib/nativeCodingAgents";

// Harness icon kinds preferred for the picker's inline list, in display order.
// A deployment that seeds none of these (OMNIGENT_SEEDED_AGENTS) backfills the
// list from whatever is launchable rather than showing an empty group.
export const PRIMARY_HARNESS_ORDER: readonly string[] = ["claude", "cursor", "codex"];
// Preferred order within the "Other..." submenu.
const SECONDARY_HARNESS_ORDER: readonly string[] = ["opencode", "pi"];
// How many rows the backfill promotes — matches the inline list's usual size.
const INLINE_HARNESS_FALLBACK = PRIMARY_HARNESS_ORDER.length;

export interface HarnessPickerInput {
  harnessEntries: readonly AvailableAgent[];
  /** Set when the deployment curates New Chat: exactly these rows, in this order. */
  curatedEntries?: readonly AvailableAgent[];
  effectiveAgentId: string | null;
  host: Host | undefined | null;
  /** Settings › Appearance: drop harnesses that can't launch on this host. */
  hideUnconfigured: boolean;
  /** Settings › Appearance: entries hidden by name; the selected one never is. */
  hiddenAgentNames: ReadonlySet<string>;
  /** A harness kept in the inline list while its config flyout is open. */
  promotedHarnessId: string | null;
}

export interface HarnessPickerChoices {
  /** The inline harness list, launch-ready first, in preferred order. */
  readyHarnesses: AvailableAgent[];
  /** The "Other..." harnesses. */
  moreHarnesses: AvailableAgent[];
  /** Whether a per-entry hide removed a harness that was otherwise offered. */
  hidOfferedHarness: boolean;
  /** The curated list as shown, when the deployment curates; it replaces the groups. */
  curated: AvailableAgent[] | undefined;
}

const rank = (agent: AvailableAgent, order: readonly string[]) => {
  const index = order.indexOf(nativeCodingAgentForAvailableAgent(agent)?.iconKind ?? "");
  return index < 0 ? order.length : index;
};

export function harnessPickerChoices(input: HarnessPickerInput): HarnessPickerChoices {
  const { effectiveAgentId, host, hideUnconfigured, hiddenAgentNames } = input;
  const isHidden = (agent: AvailableAgent) =>
    agent.id !== effectiveAgentId && hiddenAgentNames.has(agent.name);
  const cannotLaunch = (agent: AvailableAgent) => {
    const readiness = harnessReadinessOnHost(agent.harness, host);
    return !readiness.selectable && readiness.fallbackRelevant;
  };

  const ready: AvailableAgent[] = [];
  let more: AvailableAgent[] = [];
  let hidOfferedHarness = false;
  // Recorded during the split so the backfill below asks the same question
  // rather than restating it inverted.
  const unlaunchable = new Set<string>();
  for (const agent of input.harnessEntries) {
    const selected = agent.id === effectiveAgentId;
    const unavailable = cannotLaunch(agent);
    if (!selected && hideUnconfigured && unavailable) continue;
    // Readiness runs first, so this records only hides that removed an offered row.
    if (isHidden(agent)) {
      hidOfferedHarness = true;
      continue;
    }
    if (unavailable) unlaunchable.add(agent.id);
    const key = nativeCodingAgentForAvailableAgent(agent)?.iconKind ?? "";
    const primary = PRIMARY_HARNESS_ORDER.includes(key) || agent.id === input.promotedHarnessId;
    // Unavailable primaries demote to "Other..." — the main list stays
    // launch-ready; the selected harness always keeps its slot.
    if (primary && (selected || !unavailable)) ready.push(agent);
    else more.push(agent);
  }
  ready.sort(
    (first, second) => rank(first, PRIMARY_HARNESS_ORDER) - rank(second, PRIMARY_HARNESS_ORDER),
  );
  more.sort(
    (first, second) => rank(first, SECONDARY_HARNESS_ORDER) - rank(second, SECONDARY_HARNESS_ORDER),
  );
  // No preferred harness survived: promote the best launchable ones inline.
  if (ready.length === 0) {
    const promoted = more
      .filter((agent) => !unlaunchable.has(agent.id))
      .slice(0, INLINE_HARNESS_FALLBACK);
    if (promoted.length > 0) {
      ready.push(...promoted);
      const promotedIds = new Set(promoted.map((agent) => agent.id));
      more = more.filter((agent) => !promotedIds.has(agent.id));
    }
  }

  // A curated list keeps the deployment's order but honours the same per-entry
  // hides and "hide unconfigured harnesses" preference as the grouped menu, with
  // the selected row always kept so the trigger's choice stays in the list.
  const harnessIds = new Set(input.harnessEntries.map((agent) => agent.id));
  const curated = input.curatedEntries?.filter((agent) => {
    if (isHidden(agent)) return false;
    if (agent.id === effectiveAgentId || !hideUnconfigured || !harnessIds.has(agent.id)) {
      return true;
    }
    return !cannotLaunch(agent);
  });

  return { readyHarnesses: ready, moreHarnesses: more, hidOfferedHarness, curated };
}
