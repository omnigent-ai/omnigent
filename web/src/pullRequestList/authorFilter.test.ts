// Authors as the list filters to them: one login per provider and host, the Author filter's
// choices and what it keeps, and an author header's Filter.

import { describe, expect, it, vi } from "vitest";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import {
  authorFilterKey,
  authorGroupActions,
  type AuthorTarget,
  authorTargets,
} from "./authorFilter";

function entryOf(
  number: number,
  author: string | null,
  host = "github.com",
  provider?: string,
): PullRequestListEntry {
  const url = `https://${host}/acme/app/pull/${number}`;
  return {
    association: { url, host, repository: "acme/app", number, provider },
    row: { url, number, author, repository: "acme/app" },
  } as unknown as PullRequestListEntry;
}

const alice = (host: string, provider?: string): AuthorTarget => ({
  provider,
  host,
  login: "alice",
});

function actionsOf(targets: AuthorTarget[], filters = true) {
  const filter = vi.fn();
  const actions = authorGroupActions({ targets, filter: filters ? filter : undefined });
  const action = (label: string) => actions.find((candidate) => candidate.label === label)!;
  return { actions, action, filter };
}

describe("an author, by provider and host", () => {
  it("keys each login by its host, so two hosts' alice stay apart", () => {
    expect(authorFilterKey(alice("GitHub.com"))).toBe("github:github.com/alice");
    expect(authorFilterKey(alice("devices.example.com", "named-host"))).toBe(
      "named-host:devices.example.com/alice",
    );
    expect(
      authorTargets([
        entryOf(1, "alice"),
        entryOf(2, "Alice"),
        entryOf(3, "alice", "github.example.com"),
        entryOf(4, null),
      ]),
    ).toEqual([alice("github.com"), alice("github.example.com")]);
  });
});

describe("an author header's actions", () => {
  it("filters to the author, and offers no Open of their page", () => {
    const bot = { provider: undefined, host: "github.com", login: "github-actions" };
    const { actions, action, filter } = actionsOf([alice("github.com"), bot]);
    expect(actions.map((candidate) => [candidate.label, candidate.paletteLabel])).toEqual([
      ["Filter to @alice", "Filter Pull Requests to @alice"],
      ["Filter to @github-actions", "Filter Pull Requests to @github-actions"],
    ]);
    action("Filter to @alice").run();
    expect(filter).toHaveBeenCalledWith(alice("github.com"));
  });

  it("names each host where one login sits on two", () => {
    expect(
      actionsOf([alice("github.com"), alice("github.example.com")]).actions.map((a) => a.label),
    ).toEqual(["Filter to @alice on github.com", "Filter to @alice on github.example.com"]);
  });

  it("filters to another provider's author too", () => {
    expect(
      actionsOf([alice("devices.example.com", "named-host")]).actions.map((a) => a.label),
    ).toEqual(["Filter to @alice"]);
  });

  it("offers nothing where the list has no filters, or for no author", () => {
    expect(actionsOf([alice("github.com")], false).actions).toEqual([]);
    expect(actionsOf([]).actions).toEqual([]);
  });
});
