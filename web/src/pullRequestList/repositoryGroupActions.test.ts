// A repository header's links: the repositories its rows sit in by provider and host, the
// links each GitHub one gets, and none guessed for another provider.

import { describe, expect, it, vi } from "vitest";

import type { PullRequestListEntry } from "@/shell/PullRequestListRows";

import {
  repositoryGroupActions,
  type RepositoryTarget,
  repositoryTargets,
  repositoryUrl,
} from "./repositoryGroupActions";

function entryOf(host: string, repository = "acme/app", provider?: string): PullRequestListEntry {
  return {
    association: { url: `https://${host}/${repository}/pull/1`, host, repository, provider },
  } as unknown as PullRequestListEntry;
}

function actionsOf(targets: RepositoryTarget[]) {
  const open = vi.fn();
  const copy = vi.fn();
  const actions = repositoryGroupActions({ title: "acme/app", targets, open, copy });
  const action = (label: string) => actions.find((candidate) => candidate.label === label)!;
  return { actions, action, open, copy };
}

describe("a repository header's links", () => {
  it("finds each repository once by provider and host, in list order", () => {
    expect(
      repositoryTargets([
        entryOf("github.com"),
        entryOf("GitHub.example.com"),
        entryOf("github.com", "Acme/App", "github"),
      ]),
    ).toEqual([
      { provider: undefined, host: "github.com", repository: "acme/app" },
      { provider: undefined, host: "github.example.com", repository: "acme/app" },
    ]);
  });

  it("opens and copies a repository on one host as GitHub's", () => {
    const { actions, action, open, copy } = actionsOf([
      { provider: undefined, host: "github.com", repository: "acme/app" },
    ]);
    expect(actions.map((candidate) => [candidate.label, candidate.paletteLabel])).toEqual([
      ["Open Repository on GitHub", "Open acme/app on GitHub"],
      ["Copy Repository Name", "Copy Repository Name of acme/app"],
      ["Copy Repository Link", "Copy Link to acme/app on GitHub"],
    ]);
    action("Open Repository on GitHub").run();
    expect(open).toHaveBeenCalledWith("https://github.com/acme/app");
    action("Copy Repository Name").run();
    expect(copy).toHaveBeenLastCalledWith(expect.objectContaining({ text: "acme/app" }));
    action("Copy Repository Link").run();
    expect(copy).toHaveBeenLastCalledWith({
      text: "https://github.com/acme/app",
      copied: "Repository link copied",
      failed: "Couldn’t copy the repository link",
    });
  });

  it("links an Enterprise repository to its own host", () => {
    const { action, open } = actionsOf([
      { provider: "github", host: "github.example.com", repository: "acme/app" },
    ]);
    action("Open Repository on GitHub").run();
    expect(open).toHaveBeenCalledWith("https://github.example.com/acme/app");
  });

  it("names each host where one name sits on two, each opening its own", () => {
    const { actions, action, open } = actionsOf([
      { provider: undefined, host: "github.com", repository: "acme/app" },
      { provider: "github", host: "github.example.com", repository: "acme/app" },
    ]);
    expect(actions.map((candidate) => candidate.label)).toEqual([
      "Open acme/app on github.com",
      "Open acme/app on github.example.com",
      "Copy Repository Name",
      "Copy Link to acme/app on github.com",
      "Copy Link to acme/app on github.example.com",
    ]);
    action("Open acme/app on github.example.com").run();
    expect(open).toHaveBeenCalledWith("https://github.example.com/acme/app");
  });

  it("offers nothing on a header that lists no repository", () => {
    expect(actionsOf([]).actions).toEqual([]);
  });

  it("guesses no link for another provider, keeping its name", () => {
    const { actions } = actionsOf([
      { provider: "named-host", host: "3.example-host.example.com", repository: "acme/app" },
    ]);
    expect(actions.map((candidate) => candidate.label)).toEqual(["Copy Repository Name"]);
  });

  it("escapes each part of a repository's path", () => {
    expect(repositoryUrl({ provider: undefined, host: "github.com", repository: "acme/a b" })).toBe(
      "https://github.com/acme/a%20b",
    );
  });
});
