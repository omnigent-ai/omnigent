import { afterEach, expect, it, vi } from "vitest";
import { setOmnigentHostConfig, type OmnigentAnalyticsEvent } from "@/lib/host";
import { reportRecoveredCommittedItems } from "./committedItemRecovery";

afterEach(() => {
  setOmnigentHostConfig({});
  vi.restoreAllMocks();
});

it.each([0, 1])("reports only positive recovered item counts (%s)", (count) => {
  const events: OmnigentAnalyticsEvent[] = [];
  setOmnigentHostConfig({ analytics: (event) => events.push(event) });
  const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  reportRecoveredCommittedItems("poll", count, 12);
  expect(events).toEqual(
    count
      ? [{ type: "committed_items_recovered", trigger: "poll", count: 1, secondsSinceLastByte: 12 }]
      : [],
  );
  expect(warn).toHaveBeenCalledTimes(count);
});
