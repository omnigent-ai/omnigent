import { describe, expect, it } from "vitest";

import { thingyNameFrom } from "./thingyBuild";

// The derived name is the observable.
describe("thingyNameFrom", () => {
  it.each([
    ["Build a reading list for my team: books with title and author.", "Reading list for my team"],
    [
      "Please make me an incident tracker for on-call, with severity",
      "Incident tracker for on-call",
    ],
    ["weekly KPI story from a table", "Weekly KPI story from a table"],
    [
      "I want a launch checklist with owners and due dates and reminders for everyone",
      "Launch checklist with owners and due",
    ],
    ["   ", "New thingy"],
  ])("names %j %j", (description, name) => {
    expect(thingyNameFrom(description)).toBe(name);
  });
});
