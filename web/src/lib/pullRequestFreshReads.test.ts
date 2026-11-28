import { describe, expect, it } from "vitest";

import { beginFreshReads, EVERY_PULL_REQUEST, readsFresh } from "./pullRequestFreshReads";

const PR = "https://github.com/acme/app/pull/7";

describe("fresh reads of a pull request", () => {
  it("last as long as any Refresh of it, and each end counts once", () => {
    expect(readsFresh(PR)).toBe(false);
    const first = beginFreshReads(PR);
    const second = beginFreshReads(PR);
    expect(readsFresh(PR)).toBe(true);
    expect(readsFresh("https://github.com/acme/app/pull/8")).toBe(false);
    first();
    first();
    expect(readsFresh(PR)).toBe(true);
    second();
    expect(readsFresh(PR)).toBe(false);
  });

  it("covers every pull request during a whole session's Refresh", () => {
    const end = beginFreshReads(EVERY_PULL_REQUEST);
    expect(readsFresh(PR)).toBe(true);
    expect(readsFresh("https://github.com/acme/app/pull/8")).toBe(true);
    end();
    expect(readsFresh(PR)).toBe(false);
  });
});
