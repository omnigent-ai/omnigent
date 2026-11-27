import { afterEach, describe, expect, it } from "vitest";
import {
  type NamedCodeHost,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";
import type { CodeHostPullRequest } from "@/lib/codeHosts";
import { decideReadRoute, type ReadChoice, readChoiceKey } from "@/lib/pullRequestReadRoute";

const saved = pullRequestIntegrations();
afterEach(() => restorePullRequestIntegrations(saved));

const namedHost = (hostedOnGitHub: boolean): NamedCodeHost =>
  ({
    provider: "named-host",
    matchesHost: (host: string) => host === "code.example.com",
    hostedOnGitHub,
  }) as unknown as NamedCodeHost;

const target = (repository: string): CodeHostPullRequest => ({
  provider: "github",
  host: "github.com",
  repository,
  number: 7,
  url: `https://github.com/${repository}/pull/7`,
});
const AUTOMATIC = target("octo/automatic");
const FORCED = target("octo/forced");

describe("decideReadRoute", () => {
  it.each<[ReadChoice, CodeHostPullRequest | null, CodeHostPullRequest | null, unknown]>([
    ["session", AUTOMATIC, FORCED, { kind: "host" }],
    ["mac", AUTOMATIC, FORCED, { kind: "mac", target: FORCED }],
    // An explicit "This Mac" that can't read the PR is refused, never sent to the host.
    ["mac", AUTOMATIC, null, { kind: "blocked", reason: "mac_unsupported" }],
    ["automatic", AUTOMATIC, FORCED, { kind: "mac", target: AUTOMATIC }],
    ["automatic", null, FORCED, { kind: "host" }],
  ])("reads %s (automatic %o, forced %o) at %o", (choice, automatic, forced, route) => {
    expect(decideReadRoute(choice, { automatic, forced })).toEqual(route);
  });
});

describe("readChoiceKey", () => {
  it("keeps one choice for every spelling of a GitHub pull request", () => {
    expect(readChoiceKey("https://GitHub.com/Octo/Repo/pull/7/files")).toBe(
      readChoiceKey("https://github.com/octo/repo/pull/7"),
    );
  });

  it("keeps repositories apart by case on a named host GitHub doesn't run", () => {
    registerPullRequestIntegrations({ namedCodeHosts: [namedHost(false)] });
    expect(readChoiceKey("https://code.example.com/Octo/Repo/pull/7")).not.toBe(
      readChoiceKey("https://code.example.com/octo/repo/pull/7"),
    );
  });

  it("folds case on a named host that GitHub runs", () => {
    registerPullRequestIntegrations({ namedCodeHosts: [namedHost(true)] });
    expect(readChoiceKey("https://code.example.com/Octo/Repo/pull/7")).toBe(
      "https://code.example.com/octo/repo/pull/7",
    );
  });

  it("is null for a page that isn't a pull request", () => {
    expect(readChoiceKey("https://github.com/octo/repo/issues/7")).toBeNull();
  });
});
