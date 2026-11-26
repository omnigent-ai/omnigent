import { afterEach, describe, expect, it } from "vitest";
import {
  type NamedCodeHost,
  pullRequestIntegrations,
  registerPullRequestIntegrations,
  restorePullRequestIntegrations,
} from "@/lib/pullRequestIntegrations";
import { readChoiceKey } from "@/lib/pullRequestReadRoute";

const saved = pullRequestIntegrations();
afterEach(() => restorePullRequestIntegrations(saved));

const namedHost = (hostedOnGitHub: boolean): NamedCodeHost =>
  ({
    provider: "named-host",
    matchesHost: (host: string) => host === "code.example.com",
    hostedOnGitHub,
  }) as unknown as NamedCodeHost;

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
