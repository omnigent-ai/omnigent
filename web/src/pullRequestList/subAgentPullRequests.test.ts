import { describe, expect, it } from "vitest";

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";

import {
  alsoInNote,
  createConcurrencyLimit,
  mergeSubAgentPullRequests,
  type SubAgentPullRequestLinks,
} from "./subAgentPullRequests";

function pr(number: number, extra: Partial<PullRequestAssociation> = {}): PullRequestAssociation {
  return {
    url: `https://github.com/acme/app/pull/${number}`,
    host: "github.com",
    repository: "acme/app",
    number,
    relationship: "worked_on",
    ...extra,
  };
}

function member(
  id: string,
  depth: number,
  prs: PullRequestAssociation[],
  name = id,
): SubAgentPullRequestLinks {
  return { session: { id, name }, depth, prs };
}

describe("mergeSubAgentPullRequests", () => {
  it("lists a sub-agent's PR once, with that sub-agent as its source", () => {
    const { prs, sources } = mergeSubAgentPullRequests(
      [pr(1)],
      [member("child-a", 1, [pr(2)], "Auth fix")],
    );
    expect(prs.map((row) => row.number)).toEqual([1, 2]);
    expect(sources.get(pr(2).url)).toEqual({
      session: { id: "child-a", name: "Auth fix" },
      alsoIn: [],
    });
    expect(sources.has(pr(1).url)).toBe(false);
  });

  it("always shows the viewed session's own link, with the sub-agents as Also in", () => {
    const { prs, sources } = mergeSubAgentPullRequests(
      [pr(1, { first_seen_at: 500 })],
      [member("child-a", 1, [pr(1, { first_seen_at: 100 })], "Auth fix")],
    );
    expect(prs.map((row) => row.number)).toEqual([1]);
    expect(sources.get(pr(1).url)).toEqual({
      session: null,
      alsoIn: [{ id: "child-a", name: "Auth fix" }],
    });
  });

  it("gives a PR two sub-agents link to the one that linked it first", () => {
    const { prs, sources } = mergeSubAgentPullRequests(
      [],
      [
        member("child-late", 1, [pr(7, { first_seen_at: 900 })], "Late"),
        member("child-early", 1, [pr(7, { first_seen_at: 100 })], "Early"),
      ],
    );
    expect(prs).toHaveLength(1);
    expect(sources.get(pr(7).url)).toEqual({
      session: { id: "child-early", name: "Early" },
      alsoIn: [{ id: "child-late", name: "Late" }],
    });
  });

  it("breaks equal first sightings by the shallower sub-agent, then by session id", () => {
    const sameTime = { first_seen_at: 100 };
    const byDepth = mergeSubAgentPullRequests(
      [],
      [member("child-z", 1, [pr(7, sameTime)]), member("grand-a", 2, [pr(7, sameTime)])],
    );
    expect(byDepth.sources.get(pr(7).url)?.session?.id).toBe("child-z");

    const byId = mergeSubAgentPullRequests(
      [],
      [member("child-b", 1, [pr(7, sameTime)]), member("child-a", 1, [pr(7, sameTime)])],
    );
    expect(byId.sources.get(pr(7).url)?.session?.id).toBe("child-a");
  });

  it("ranks a link with no first sighting after every timed one", () => {
    const { sources } = mergeSubAgentPullRequests(
      [],
      [
        member("child-untimed", 1, [pr(7, { first_seen_at: null })]),
        member("child-timed", 3, [pr(7, { first_seen_at: 9_999 })]),
      ],
    );
    expect(sources.get(pr(7).url)?.session?.id).toBe("child-timed");
  });

  it("ignores later activity when choosing the winner", () => {
    const { sources } = mergeSubAgentPullRequests(
      [],
      [
        member("child-a", 1, [pr(7, { first_seen_at: 100, last_seen_at: 100 })]),
        member("child-b", 1, [pr(7, { first_seen_at: 200, last_seen_at: 9_000 })]),
      ],
    );
    expect(sources.get(pr(7).url)?.session?.id).toBe("child-a");
  });

  it("matches PRs by their canonical URL", () => {
    const { prs } = mergeSubAgentPullRequests(
      [pr(3)],
      [member("child-a", 1, [pr(3, { url: "https://GitHub.com/acme/app/pull/3/" })])],
    );
    expect(prs).toHaveLength(1);
  });
});

describe("alsoInNote", () => {
  it("names one, two, or more other sessions", () => {
    expect(alsoInNote([])).toBeNull();
    expect(alsoInNote([{ id: "a", name: "Auth" }])).toBe("Also in Auth");
    expect(
      alsoInNote([
        { id: "a", name: "Auth" },
        { id: "b", name: "Docs" },
      ]),
    ).toBe("Also in Auth and Docs");
    expect(
      alsoInNote([
        { id: "a", name: "Auth" },
        { id: "b", name: "Docs" },
        { id: "c", name: "Tests" },
      ]),
    ).toBe("Also in Auth and 2 more");
  });
});

describe("createConcurrencyLimit", () => {
  it("never runs more tasks at once than its limit, and finishes them all", async () => {
    const limit = createConcurrencyLimit(2);
    let running = 0;
    let peak = 0;
    const task = (value: number) =>
      limit(async () => {
        running += 1;
        peak = Math.max(peak, running);
        await new Promise((resolve) => {
          setTimeout(resolve, 5);
        });
        running -= 1;
        return value;
      });
    const results = await Promise.all([1, 2, 3, 4, 5].map(task));
    expect(results).toEqual([1, 2, 3, 4, 5]);
    expect(peak).toBe(2);
  });

  it("keeps the limit when a task fails", async () => {
    const limit = createConcurrencyLimit(1);
    await expect(limit(async () => Promise.reject(new Error("boom")))).rejects.toThrow("boom");
    await expect(limit(async () => "ok")).resolves.toBe("ok");
  });
});
