import { afterEach, describe, expect, it } from "vitest";

import { usePullRequestListCounts } from "./pullRequestListCounts";

afterEach(() => usePullRequestListCounts.setState({ counts: {} }));

describe("usePullRequestListCounts", () => {
  const counts = () => usePullRequestListCounts.getState().counts;
  const { publish, forget } = usePullRequestListCounts.getState();

  it("keeps each session's latest count apart", () => {
    publish("a", 3);
    publish("b", 1);
    publish("a", 2);
    expect(counts()).toEqual({ a: 2, b: 1 });
  });

  it("keeps the same state for an unchanged count, so readers don't rerender", () => {
    publish("a", 3);
    const before = counts();
    publish("a", 3);
    expect(counts()).toBe(before);
  });

  it("forgets a session's count once its list is gone, leaving the others", () => {
    publish("a", 3);
    publish("b", 1);
    forget("a");
    expect(counts()).toEqual({ b: 1 });
    const before = counts();
    forget("a");
    expect(counts()).toBe(before);
  });
});
