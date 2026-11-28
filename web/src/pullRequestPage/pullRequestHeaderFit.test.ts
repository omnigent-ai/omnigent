import { describe, expect, it } from "vitest";

import { fitPullRequestHeader, type PullRequestHeaderMeasurements } from "./pullRequestHeaderFit";

// A header's parts: an 8px gap, a 140/90/40px name, an 80px status and 300px of tabs, with no
// read source to show.
const PARTS: Omit<PullRequestHeaderMeasurements, "available"> = {
  gap: 8,
  lead: 0,
  names: { full: 140, repository: 90, number: 40 },
  badges: { full: 80, icon: 40 },
  status: { full: 0, short: 0, icon: 0 },
  source: { full: 0, short: 0, icon: 0 },
  sections: 300,
  actions: 0,
};

// Where the PR is read: 150px with the whole account, 70px with the place alone, 30px as an icon.
const SOURCE = { source: { full: 150, short: 70, icon: 30 } };

const fit = (available: number, over: Partial<PullRequestHeaderMeasurements> = {}) =>
  fitPullRequestHeader({ ...PARTS, ...over, available });

describe("fitPullRequestHeader", () => {
  it.each([
    // The sections always have a row of their own, so every label fits from 300px.
    [1000, "full", "segments"],
    [300, "full", "segments"],
    // Too narrow for every label: a picker shows the current section.
    [299, "full", "menu"],
    // The top row gives up the owner, then the repository.
    [140 + 80 + 8, "full", "menu"],
    [140 + 80 + 7, "repository", "menu"],
    [90 + 80 + 8, "repository", "menu"],
    [90 + 80 + 7, "number", "menu"],
  ])("without a read source, at %ipx: %s name, %s", (available, name, sections) => {
    expect(fit(available)).toEqual({
      name,
      spacing: "roomy",
      lifecycle: "full",
      stack: "full",
      status: "full",
      source: "full",
      sections,
    });
  });

  it("makes room for a stack's switcher and chip, keeping only its pancake when narrowest", () => {
    const stacked = { stack: { full: 100, compact: 30 } };
    // Name, pill and stack with two gaps: the owner goes first, as without a stack.
    expect(fit(140 + 80 + 100 + 16, stacked)).toMatchObject({ name: "full", stack: "full" });
    expect(fit(140 + 80 + 100 + 15, stacked)).toMatchObject({ name: "repository", stack: "full" });
    // Past the number alone, the pill's icon and the switcher's pancake.
    expect(fit(40 + 80 + 100 + 15, stacked)).toMatchObject({
      name: "number",
      lifecycle: "icon",
      stack: "compact",
    });
  });

  it.each([
    // Name, status, source, two gaps, and the source's separation from the status.
    [140 + 80 + 150 + 16 + 16, "full", "full"],
    // The owner gives way before the account does.
    [140 + 80 + 150 + 16 + 15, "repository", "full"],
    [90 + 80 + 150 + 16 + 16, "repository", "full"],
    // Then the account truncates, leaving the place in view, before the repository goes.
    [90 + 80 + 150 + 16 + 15, "repository", "short"],
    [90 + 80 + 70 + 16 + 16, "repository", "short"],
    [90 + 80 + 70 + 16 + 15, "number", "short"],
    [40 + 80 + 70 + 16 + 16, "number", "short"],
    // Narrower still, the source keeps its icon, never clipped.
    [40 + 80 + 70 + 16 + 15, "number", "icon"],
    [40 + 80 + 30 + 16 + 16, "number", "icon"],
    [100, "number", "icon"],
  ])("with a read source, at %ipx: %s name, %s source", (available, name, source) => {
    expect(fit(available, SOURCE)).toMatchObject({ name, source });
  });

  it.each([
    // Name, status, the lifecycle, source, three gaps and the source's separation.
    [140 + 80 + 120 + 150 + 24 + 16, "full", "full", "full"],
    // The owner goes first, then the status's long label, then the account.
    [140 + 80 + 120 + 150 + 24 + 15, "repository", "full", "full"],
    [90 + 80 + 120 + 150 + 24 + 15, "repository", "short", "full"],
    [90 + 80 + 60 + 150 + 24 + 15, "repository", "short", "short"],
    [90 + 80 + 60 + 70 + 24 + 15, "number", "short", "short"],
    [40 + 80 + 60 + 70 + 24 + 15, "number", "short", "icon"],
    // Last, the status keeps just its icon, its label in its tooltip and name.
    [40 + 80 + 60 + 30 + 24 + 15, "number", "icon", "icon"],
    [40 + 80 + 28 + 30 + 24 + 16, "number", "icon", "icon"],
  ])(
    "with a merge status, at %ipx: %s name, %s status, %s source",
    (available, name, status, source) => {
      const parts = { ...SOURCE, status: { full: 120, short: 60, icon: 28 } };
      expect(fit(available, parts)).toMatchObject({ name, status, source });
    },
  );

  it("keeps the lifecycle pill's icon and gives up the source's room last", () => {
    const parts = { ...SOURCE, status: { full: 120, short: 60, icon: 28 } };
    expect(fit(40 + 80 + 28 + 30 + 24 + 16, parts)).toMatchObject({ lifecycle: "full" });
    // A pixel narrower, the pill keeps its icon, and the row's parts sit at half their gap,
    // the source right beside the status.
    expect(fit(40 + 80 + 28 + 30 + 24 + 15, parts)).toMatchObject({
      name: "number",
      spacing: "tight",
      lifecycle: "icon",
      status: "icon",
      source: "icon",
    });
    expect(fit(40 + 40 + 28 + 30 + 12, parts)).toMatchObject({ lifecycle: "icon" });
    // Narrower than even that, the number truncates in the same arrangement.
    expect(fit(100, parts)).toMatchObject({ name: "number", lifecycle: "icon", status: "icon" });
    // Without a status or source, the pill gives way only after the repository.
    expect(fit(40 + 80 + 8)).toMatchObject({ name: "number", lifecycle: "full" });
    expect(fit(40 + 80 + 7)).toMatchObject({ name: "number", lifecycle: "icon" });
  });

  it("keeps a status whose short label is no shorter at its full length", () => {
    const parts = { ...SOURCE, status: { full: 70, short: 70, icon: 28 } };
    expect(fit(90 + 80 + 70 + 150 + 24 + 16, parts)).toMatchObject({
      name: "repository",
      status: "full",
      source: "full",
    });
    expect(fit(90 + 80 + 70 + 150 + 24 + 15, parts)).toMatchObject({
      name: "repository",
      status: "full",
      source: "short",
    });
  });

  it("keeps the way back with the name, and the actions with the sections", () => {
    const parts = { ...SOURCE, lead: 30, actions: 60 };
    expect(fit(30 + 140 + 80 + 150 + 8 * 3 + 16, parts)).toMatchObject({
      name: "full",
      source: "full",
    });
    expect(fit(30 + 140 + 80 + 150 + 8 * 3 + 15, parts)).toMatchObject({ name: "repository" });
    // The actions sit beside the tabs on their row; there, every label needs room for them too.
    expect(fit(300 + 60 + 8, parts)).toMatchObject({ sections: "segments" });
    expect(fit(300 + 60 + 7, parts)).toMatchObject({ sections: "menu" });
  });

  it("fits to the exact width, and steps down when a row is a fraction of a pixel wider", () => {
    const exact = 140 + 80 + 150 + 16 + 16;
    expect(fit(exact, SOURCE)).toMatchObject({ name: "full", source: "full" });
    // The browser wraps a row only 1/8 px too wide, so the fit gives way first.
    expect(fit(exact - 0.125, SOURCE)).toMatchObject({ name: "repository", source: "full" });
  });
});
