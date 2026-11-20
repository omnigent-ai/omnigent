import { describe, expect, it } from "vitest";

import { composeThingyBuildPrompt, thingyNameFrom } from "./thingyBuild";

// The derived name is the observable.
describe("thingyNameFrom", () => {
  it.each([
    ["Build a reading list for my team: books with title and author.", "Reading list for my team"],
    [
      "Please make me an incident tracker for on-call, with severity",
      "Incident tracker for on-call",
    ],
    ["weekly KPI story from a table", "Weekly KPI story from a table"],
    ["A weekly KPI story from a table", "A weekly KPI story from a table"],
    [
      "I want a launch checklist with owners and due dates and reminders for everyone",
      "Launch checklist with owners and due dates and reminders",
    ],
    ["Incident tracker for my team…", "Incident tracker for my team"],
    ["Incident tracker... with owners", "Incident tracker"],
    ["I'd like a board for my team's OKRs", "Board for my team's OKRs"],
    ["Track e.g. bugs per team. Then more.", "Track e.g. bugs per team"],
    ["A U.S. holiday calendar", "A U.S. holiday calendar"],
    ["https://example.com/data.csv as a chart", "https://example.com/data.csv as a chart"],
    ["Hi, can you build a release tracker?", "Release tracker"],
    ["Notes!!!", "Notes"],
    ["...", "New thingy"],
    ["   ", "New thingy"],
  ])("names %j %j", (description, name) => {
    expect(thingyNameFrom(description)).toBe(name);
  });

  it("stops at a whole word within the length budget", () => {
    const name = thingyNameFrom(`A ${"verylongword ".repeat(10)}tracker`);

    expect(name.length).toBeLessThanOrEqual(60);
    expect(name.endsWith("verylongword")).toBe(true);
  });
});

describe("composeThingyBuildPrompt", () => {
  const APP = { id: "app-1", name: "Reading list" };

  it("starts with a short note naming the thingy and the skill, then the description", () => {
    const prompt = composeThingyBuildPrompt(APP, "  A reading list with title and author  ");
    const [note, description] = prompt.split("\n\n");

    expect(note.startsWith("Load the thingy skill and follow it.")).toBe(true);
    expect(note).toContain('"Reading list" (thingy_id app-1)');
    expect(note.length).toBeLessThan(300);
    expect(description).toBe("A reading list with title and author");
  });

  it("leaves a blank thingy's prompt ready for the description, without an empty heading", () => {
    const prompt = composeThingyBuildPrompt(APP);

    expect(prompt).toContain("from my description below");
    expect(prompt).not.toMatch(/description:\s*$/i);
    expect(prompt.endsWith("\n\n")).toBe(true);
  });
});
