// Which list rows read GitHub: rows that stay in view, spread out to a few new reads a minute,
// plus the rows the user is working with; never a row scrolled past or merely selected.

import { describe, expect, it } from "vitest";

import {
  FIRST_READ_BATCH_WAIT_MS,
  FirstReadSchedule,
  MAX_READ_EXCEPTIONS,
  NEW_ROW_READS_PER_MINUTE,
  READ_TARGET_DWELL_MS,
  ReadTargetSchedule,
} from "./useListReadTargets";

const urls = (count: number, from = 1) =>
  Array.from({ length: count }, (_value, index) => `pr${from + index}`);

const input = (visible: readonly string[], overrides = {}) => ({
  visible,
  exceptions: [] as string[],
  needsRead: () => true,
  allowed: true,
  ...overrides,
});

/** A schedule whose list opened on other rows, so the rows a test shows next came by a scroll. */
function scrolled() {
  const schedule = new ReadTargetSchedule();
  schedule.update(input(["opened"], { needsRead: () => false }), -1_000);
  return schedule;
}

describe("the list's read targets", () => {
  it("reads the screen the list opens on at once, with no scroll to wait out", () => {
    const schedule = new ReadTargetSchedule();
    expect([...schedule.update(input(urls(3)), 0).targets]).toEqual(urls(3));
    // Emptied by a filter and filled again, the view reads at once too.
    schedule.update(input([]), 1_000);
    expect([...schedule.update(input(urls(2, 50)), 2_000).targets]).toEqual(urls(2, 50));
  });

  it("reads a row a scroll brings in only once it has stayed in view for a moment", () => {
    const schedule = scrolled();
    const first = schedule.update(input(urls(3)), 0);
    expect([...first.targets]).toEqual([]);
    expect(first.wakeAt).toBe(READ_TARGET_DWELL_MS);
    expect([...schedule.update(input(urls(3)), READ_TARGET_DWELL_MS).targets]).toEqual(urls(3));
  });

  it("reads nothing for rows a fast scroll passes", () => {
    const schedule = scrolled();
    // 300 rows pass, 20 at a time, every 50 ms.
    for (let step = 0; step < 15; step++) {
      const shown = schedule.update(input(urls(20, 1 + step * 20)), step * 50);
      expect(shown.targets.size).toBe(0);
    }
  });

  it("starts at most 30 new reads a minute, and the rest wait their turn", () => {
    const schedule = scrolled();
    const visible = urls(40);
    schedule.update(input(visible), 0);
    const settled = schedule.update(input(visible), READ_TARGET_DWELL_MS);
    expect(settled.targets.size).toBe(NEW_ROW_READS_PER_MINUTE);
    // The next one's turn comes a minute after the first read.
    expect(settled.wakeAt).toBe(READ_TARGET_DWELL_MS + 60_000);
    expect(schedule.update(input(visible), 30_000).targets.size).toBe(30);
    expect(schedule.update(input(visible), READ_TARGET_DWELL_MS + 60_000).targets.size).toBe(40);
  });

  it("drops a waiting row that left the view, and a returning row waits again", () => {
    const schedule = new ReadTargetSchedule();
    schedule.update(input(urls(40)), 0);
    schedule.update(input(urls(40)), READ_TARGET_DWELL_MS);
    // The 10 waiting rows scroll away; 10 new rows come in and wait behind the limit too.
    const moved = [...urls(30), ...urls(10, 100)];
    schedule.update(input(moved), 1_000);
    const later = schedule.update(input(moved), READ_TARGET_DWELL_MS + 60_000);
    expect([...later.targets].filter((url) => Number(url.slice(2)) >= 100)).toHaveLength(10);
    expect(later.targets.has("pr31")).toBe(false);
  });

  it("lets a row whose cached answer is fresh show it without spending a read", () => {
    const schedule = new ReadTargetSchedule();
    const visible = urls(60);
    const fresh = input(visible, { needsRead: (url: string) => Number(url.slice(2)) > 30 });
    schedule.update(fresh, 0);
    // 30 fresh rows cost nothing, and 30 that need a read fill the minute.
    expect(schedule.update(fresh, READ_TARGET_DWELL_MS).targets.size).toBe(60);
  });

  it("while the app may not read on its own, reads only the exceptions, then resumes", () => {
    const schedule = new ReadTargetSchedule();
    const held = input(urls(5), { allowed: false, exceptions: ["open-pr"] });
    schedule.update(held, 0);
    expect([...schedule.update(held, READ_TARGET_DWELL_MS).targets]).toEqual(["open-pr"]);
    const resumed = schedule.update({ ...held, allowed: true }, 1_000);
    expect(resumed.targets.size).toBe(6);
  });

  it("reads at most three exceptions, at once and wherever they are", () => {
    const schedule = new ReadTargetSchedule();
    const many = input([], { exceptions: urls(5, 900) });
    expect(schedule.update(many, 0).targets.size).toBe(MAX_READ_EXCEPTIONS);
  });
});

describe("a row's first read after its batch", () => {
  const targets = new Set(["a", "b"]);

  it("reads at once a row that needn't wait, and holds one that waits for its batch", () => {
    const schedule = new FirstReadSchedule();
    const first = schedule.update({ targets, waits: (url) => url === "b" }, 0);
    expect([...first.targets]).toEqual(["a"]);
    expect(first.wakeAt).toBe(FIRST_READ_BATCH_WAIT_MS);
    // Its batch answers: it reads.
    expect([...schedule.update({ targets, waits: () => false }, 1_000).targets]).toEqual([
      "a",
      "b",
    ]);
  });

  it("reads anyway once the wait runs out, so a batch that never comes holds nothing back", () => {
    const schedule = new FirstReadSchedule();
    schedule.update({ targets, waits: () => true }, 0);
    expect(schedule.update({ targets, waits: () => true }, 5_000).targets.size).toBe(0);
    const late = schedule.update({ targets, waits: () => true }, FIRST_READ_BATCH_WAIT_MS);
    expect([...late.targets]).toEqual(["a", "b"]);
    expect(late.wakeAt).toBeNull();
  });

  it("starts the wait again for a row that left the targets and came back", () => {
    const schedule = new FirstReadSchedule();
    schedule.update({ targets, waits: () => true }, 0);
    schedule.update({ targets: new Set(["a"]), waits: () => true }, 4_000);
    const back = schedule.update({ targets, waits: () => true }, FIRST_READ_BATCH_WAIT_MS);
    expect([...back.targets]).toEqual(["a"]);
    expect(back.wakeAt).toBe(FIRST_READ_BATCH_WAIT_MS * 2);
  });
});
