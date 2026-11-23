// Which entries the flat tab strip shows, and how each is moving: a tab added after the
// strip first showed grows in, and a closed one stays briefly as a ghost that shrinks
// away at its old place. Where each is on screen is the browser's: a ghost is the closed
// tab's own element, so CSS eases it from the width it has. The caller supplies the clock.

import type { TabStripMotion } from "./tabStripMotion";

export type PresencePhase = "present" | "entering" | "leaving";

export interface PresenceSlot<Payload> {
  key: string;
  phase: PresencePhase;
  /** When the current phase began, on the caller's clock (ms). */
  startedAt: number;
  /** A ghost's last live payload, which it keeps showing as it leaves. */
  payload: Payload | undefined;
}

export interface PresenceState<Payload> {
  /** Live entries in their order, with ghosts at the places their tabs left. */
  slots: readonly PresenceSlot<Payload>[];
}

function present<Payload>(key: string): PresenceSlot<Payload> {
  return { key, phase: "present", startedAt: 0, payload: undefined };
}

/** The first showing: every tab is simply there, with nothing to animate. */
export function initialPresence<Payload>(keys: readonly string[]): PresenceState<Payload> {
  return { slots: keys.map((key) => present<Payload>(key)) };
}

/**
 * The strip after its live keys became `keys`. A key that took a removed key's place in the
 * same change is a replacement and shows at once, as when a New tab turns into the tool
 * opened from it. `ghostPayload` gives a removed key the payload its ghost keeps showing.
 */
export function updatePresence<Payload>(
  state: PresenceState<Payload>,
  keys: readonly string[],
  now: number,
  ghostPayload: (key: string) => Payload | undefined,
): PresenceState<Payload> {
  const previous = state.slots;
  // The first tabs of an empty strip arrive with the strip itself, so they simply show.
  if (previous.length === 0) return initialPresence(keys);
  const previousLive = previous.filter((slot) => slot.phase !== "leaving");
  const previousLiveKeys = new Set(previousLive.map((slot) => slot.key));
  const nextKeys = new Set(keys);
  const bySlotKey = new Map(previous.map((slot) => [slot.key, slot]));

  // A key new at index i, where the old key at i was removed, replaces it in place.
  const replacements = new Set<string>();
  const replaced = new Set<string>();
  keys.forEach((key, index) => {
    const old = previousLive[index];
    if (previousLiveKeys.has(key) || old === undefined || nextKeys.has(old.key)) return;
    if (bySlotKey.get(key)?.phase === "leaving") return;
    replacements.add(key);
    replaced.add(old.key);
  });

  const live: PresenceSlot<Payload>[] = keys.map((key) => {
    const existing = bySlotKey.get(key);
    if (existing && existing.phase !== "leaving") return existing;
    if (replacements.has(key)) return present<Payload>(key);
    // New, or a ghost brought back: its element grows from whatever width it has.
    return { key, phase: "entering", startedAt: now, payload: undefined };
  });

  const ghosts = new Map<string, PresenceSlot<Payload>>();
  for (const slot of previous) {
    if (nextKeys.has(slot.key) || replaced.has(slot.key)) continue;
    if (slot.phase === "leaving") {
      ghosts.set(slot.key, slot);
      continue;
    }
    const payload = ghostPayload(slot.key);
    if (payload === undefined) continue;
    ghosts.set(slot.key, { key: slot.key, phase: "leaving", startedAt: now, payload });
  }

  // Each ghost goes back after the nearest slot that came before it and is still shown.
  const slots = [...live];
  let anchor = -1;
  for (const slot of previous) {
    const ghost = ghosts.get(slot.key);
    if (ghost) {
      slots.splice(anchor + 1, 0, ghost);
      anchor += 1;
      continue;
    }
    const at = slots.findIndex((candidate) => candidate.key === slot.key);
    if (at !== -1) anchor = at;
  }
  return { slots };
}

/** Ends the phases that have run their course at `now`: ghosts go, entering tabs settle. */
export function settlePresence<Payload>(
  state: PresenceState<Payload>,
  now: number,
  motion: TabStripMotion,
): PresenceState<Payload> {
  let changed = false;
  const slots: PresenceSlot<Payload>[] = [];
  for (const slot of state.slots) {
    if (slot.phase === "present" || now < phaseEnd(slot, motion)) {
      slots.push(slot);
      continue;
    }
    changed = true;
    if (slot.phase === "entering") slots.push(present<Payload>(slot.key));
  }
  return changed ? { slots } : state;
}

/** Ends every phase at once, as when the user turns on Reduce motion mid-animation. */
export function settleAllPresence<Payload>(state: PresenceState<Payload>): PresenceState<Payload> {
  if (state.slots.every((slot) => slot.phase === "present")) return state;
  return {
    slots: state.slots
      .filter((slot) => slot.phase !== "leaving")
      .map((slot) => (slot.phase === "present" ? slot : present<Payload>(slot.key))),
  };
}

/** When the next phase ends, or null when nothing is moving. */
export function nextPresenceDeadline<Payload>(
  state: PresenceState<Payload>,
  motion: TabStripMotion,
): number | null {
  let deadline: number | null = null;
  for (const slot of state.slots) {
    if (slot.phase === "present") continue;
    const end = phaseEnd(slot, motion);
    if (deadline === null || end < deadline) deadline = end;
  }
  return deadline;
}

/** When a slot's phase is over, including the slack for its transition's late start. */
function phaseEnd<Payload>(slot: PresenceSlot<Payload>, motion: TabStripMotion): number {
  const duration = slot.phase === "entering" ? motion.enterMs : motion.leaveMs;
  return slot.startedAt + duration + motion.settleSlackMs;
}
