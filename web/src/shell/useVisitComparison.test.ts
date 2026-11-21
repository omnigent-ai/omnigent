import { renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { ComparisonChoice } from "@/lib/comparisonChoice";

import {
  type ComparisonAvailability,
  useVisitComparison,
  type VisitComparisonOptions,
} from "./useVisitComparison";

const AUTOMATIC: ComparisonChoice = { kind: "saved", intent: "automatic" };
const BRANCH: ComparisonChoice = { kind: "saved", intent: "branch-point" };
const TYPED: ComparisonChoice = { kind: "typed", base: "main", head: "" };

interface Step {
  active?: boolean;
  visit?: ComparisonChoice;
  landing?: { settled: boolean };
  shown?: ComparisonChoice;
  settled?: boolean;
  availability?: ComparisonAvailability;
  choices?: number;
}

function setup(initial: Step = {}) {
  const choose = vi.fn();
  const onShown = vi.fn();
  const props = (step: Step): VisitComparisonOptions => ({
    active: step.active ?? true,
    visit: step.visit,
    landing: step.landing,
    session: {
      shown: step.shown ?? AUTOMATIC,
      settled: step.settled ?? true,
      availability: () => step.availability ?? "now",
      choose,
    },
    choices: step.choices ?? 0,
    onShown,
  });
  const view = renderHook((current: VisitComparisonOptions) => useVisitComparison(current), {
    initialProps: props(initial),
  });
  return {
    choose,
    onShown,
    result: view.result,
    rerender: (step: Step) => view.rerender(props(step)),
  };
}

const landed = () => ({ settled: false });

describe("useVisitComparison", () => {
  it("records what a visit first shows on it, in place", () => {
    const { choose, onShown } = setup({ shown: BRANCH });
    expect(onShown).toHaveBeenCalledWith(BRANCH, { step: false });
    expect(choose).not.toHaveBeenCalled();
  });

  it("waits for the session's typed comparison to be known before recording anything", () => {
    const { onShown, rerender } = setup({ settled: false });
    expect(onShown).not.toHaveBeenCalled();
    rerender({ shown: TYPED });
    expect(onShown).toHaveBeenCalledWith(TYPED, { step: false });
  });

  it("makes a comparison the person picks a step", () => {
    const { onShown, rerender } = setup({ visit: AUTOMATIC });
    expect(onShown).not.toHaveBeenCalled();
    rerender({ visit: AUTOMATIC, shown: BRANCH, choices: 1 });
    expect(onShown).toHaveBeenCalledWith(BRANCH, { step: true });
  });

  it("takes a comparison chosen elsewhere in place, with no step", () => {
    const { onShown, rerender } = setup({ visit: AUTOMATIC });
    rerender({ visit: AUTOMATIC, shown: TYPED });
    expect(onShown).toHaveBeenCalledWith(TYPED, { step: false });
  });

  it("brings a visit's comparison back when Back lands on it, without counting it as a pick", () => {
    const { choose, onShown, rerender } = setup({ visit: BRANCH, shown: BRANCH });
    const back = landed();
    rerender({ visit: AUTOMATIC, landing: back, shown: BRANCH });
    expect(choose).toHaveBeenCalledWith(AUTOMATIC);
    expect(back.settled).toBe(true);
    rerender({ visit: AUTOMATIC, landing: back, shown: AUTOMATIC, choices: 1 });
    expect(onShown).not.toHaveBeenCalled();
  });

  it("brings it back when Back lands on a tab that already shows the visit", () => {
    const first = landed();
    const { choose, rerender } = setup({ visit: BRANCH, landing: first, shown: BRANCH });
    rerender({ visit: BRANCH, landing: first, shown: TYPED });
    rerender({ visit: BRANCH, landing: landed(), shown: TYPED });
    expect(choose).toHaveBeenCalledWith(BRANCH);
  });

  it("applies a linked visit's comparison when its tab first shows", () => {
    const { choose, onShown } = setup({ visit: TYPED, landing: landed() });
    expect(choose).toHaveBeenCalledWith(TYPED);
    expect(onShown).not.toHaveBeenCalled();
  });

  it("keeps what the session shows when a tab shows again without landing, as after a reload", () => {
    const settled = { settled: true };
    const { choose, onShown } = setup({ visit: AUTOMATIC, landing: settled, shown: TYPED });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).toHaveBeenCalledWith(TYPED, { step: false });
  });

  it("does nothing while the review is off screen, and takes what the session shows once back", () => {
    const { choose, onShown, rerender } = setup({ visit: BRANCH, shown: BRANCH });
    rerender({ active: false, visit: BRANCH, shown: TYPED, choices: 1 });
    expect(onShown).not.toHaveBeenCalled();
    rerender({ active: true, visit: BRANCH, shown: TYPED, choices: 1 });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).toHaveBeenCalledWith(TYPED, { step: false });
  });

  it("waits until the session can show a landing's comparison, and keeps the visit meanwhile", () => {
    const link = landed();
    const { choose, onShown, rerender } = setup({
      visit: TYPED,
      landing: link,
      availability: "later",
    });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).not.toHaveBeenCalled();
    rerender({ visit: TYPED, landing: link, availability: "now" });
    expect(choose).toHaveBeenCalledWith(TYPED);
  });

  it("keeps a landing's comparison the session can't show, and says why, as the menu would", () => {
    const { choose, onShown, result } = setup({
      visit: BRANCH,
      landing: landed(),
      availability: { unavailable: "Not on this host." },
    });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).not.toHaveBeenCalled();
    expect(result.current).toEqual({
      unavailable: { choice: BRANCH, reason: "Not on this host." },
    });
  });

  it("lets a pick made while a landing waits win, as a step, over the landing's comparison", () => {
    const link = landed();
    const { choose, onShown, rerender } = setup({
      visit: TYPED,
      landing: link,
      availability: "later",
    });
    rerender({ visit: TYPED, landing: link, availability: "later", shown: BRANCH, choices: 1 });
    expect(onShown).toHaveBeenCalledWith(BRANCH, { step: true });
    rerender({ visit: TYPED, landing: link, availability: "now", shown: BRANCH, choices: 1 });
    expect(choose).not.toHaveBeenCalled();
  });

  it("keeps a pick made before the session is known over a waiting landing's comparison", () => {
    const link = landed();
    const { choose, onShown, rerender } = setup({ visit: TYPED, landing: link, settled: false });
    rerender({ visit: TYPED, landing: link, settled: false, shown: BRANCH, choices: 1 });
    rerender({ visit: TYPED, landing: link, shown: BRANCH, choices: 1 });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).toHaveBeenCalledWith(BRANCH, { step: true });
  });

  it("keeps a pick that arrives with the session's readiness over a waiting landing's comparison", () => {
    const link = landed();
    const { choose, onShown, rerender } = setup({
      visit: TYPED,
      landing: link,
      availability: "later",
    });
    rerender({ visit: TYPED, landing: link, availability: "now", shown: BRANCH, choices: 1 });
    expect(choose).not.toHaveBeenCalled();
    expect(onShown).toHaveBeenCalledWith(BRANCH, { step: true });
  });
});
