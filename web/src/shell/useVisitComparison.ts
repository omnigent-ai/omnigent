// A workspace tab's visit to Changes keeps the comparison it showed. Landing on the visit again,
// by Back, Forward or a link, applies that comparison to the session, so the badge, the list
// and every review agree; a comparison the person picks is a new step.

import { useEffect, useRef } from "react";

import { type ComparisonChoice, sameComparisonChoice } from "@/lib/comparisonChoice";

/** Whether the session can show a comparison: now, once it knows more, or not here and why. */
export type ComparisonAvailability = "now" | "later" | { unavailable: string };

/** A landing's comparison the session can't show, kept on the visit, and why. */
export interface UnavailableVisitComparison {
  unavailable: { choice: ComparisonChoice; reason: string };
}

export interface VisitComparisonOptions {
  /** The review is on screen; only then does it apply or record a comparison. */
  active: boolean;
  /** The comparison the visit showed, if it recorded one. */
  visit: ComparisonChoice | undefined;
  /**
   * How the tab last landed on the visit. Its comparison is applied once per landing; a tab
   * shown again without one, such as by a click or after a reload, keeps what the session shows.
   */
  landing: { settled: boolean } | undefined;
  /** What the session shows, and how to apply a comparison to it. */
  session: {
    shown: ComparisonChoice;
    settled: boolean;
    availability: (choice: ComparisonChoice) => ComparisonAvailability;
    choose: (choice: ComparisonChoice) => void;
  };
  /** Changes each time the person chooses a comparison for the session. */
  choices: number;
  /**
   * The session shows a comparison the visit didn't record: `step` when the person picked
   * it here, else the visit takes it in place, as when it first shows or another window chose.
   */
  onShown: (shown: ComparisonChoice, options: { step: boolean }) => void;
}

function choiceKey(choice: ComparisonChoice | undefined): string {
  return JSON.stringify(choice ?? null);
}

export function useVisitComparison({
  active,
  visit,
  landing,
  session,
  choices,
  onShown,
}: VisitComparisonOptions): UnavailableVisitComparison | null {
  const visitKey = choiceKey(visit);
  const shownKey = choiceKey(session.shown);
  const availability = visit === undefined ? "now" : session.availability(visit);
  const availabilityKey = typeof availability === "string" ? availability : "unavailable";
  // The choice count when this review last asked the session for a visit's comparison.
  const applying = useRef<number | null>(null);
  // The choice count last recorded; a newer one is a pick still to record.
  const seenChoices = useRef(choices);
  const latest = useRef({ visit, session, onShown });
  latest.current = { visit, session, onShown };

  // A landing brings back the visit's comparison, once the session can show it.
  useEffect(() => {
    if (!active || !session.settled || !landing || landing.settled) return;
    const { visit: wanted, session: current } = latest.current;
    if (wanted === undefined || sameComparisonChoice(wanted, current.shown)) {
      landing.settled = true;
      return;
    }
    // A comparison that can't show yet waits, and one that can't show here stays asked for.
    if (availability !== "now") return;
    landing.settled = true;
    // A pick since the landing is newer; the effect below records it as a step.
    if (seenChoices.current !== choices) return;
    applying.current = choices;
    current.choose(wanted);
  }, [active, session.settled, landing, visitKey, availabilityKey]); // eslint-disable-line react-hooks/exhaustive-deps

  // What the session shows otherwise is recorded on the visit.
  useEffect(() => {
    // A pick off screen is taken in place; one before the session is known waits to be recorded.
    if (active && !session.settled) return;
    const picked = seenChoices.current !== choices;
    seenChoices.current = choices;
    if (!active) return;
    // A landing still waiting to show its comparison keeps the visit as it is, unless the
    // person picks another comparison meanwhile: theirs is newer, and a step.
    if (landing && !landing.settled) {
      if (!picked) return;
      landing.settled = true;
    }
    const { visit: recorded, session: current } = latest.current;
    if (sameComparisonChoice(recorded, current.shown)) {
      applying.current = null;
      return;
    }
    // The applied comparison is still on its way; its own choice doesn't count as a pick.
    if (applying.current !== null && choices <= applying.current + 1) return;
    applying.current = null;
    latest.current.onShown(current.shown, { step: picked && recorded !== undefined });
  }, [active, session.settled, landing, visitKey, shownKey, choices, availabilityKey]);

  const waiting = active && session.settled && landing !== undefined && !landing.settled;
  return waiting && visit !== undefined && typeof availability !== "string"
    ? { unavailable: { choice: visit, reason: availability.unavailable } }
    : null;
}
