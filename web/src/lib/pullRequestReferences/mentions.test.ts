import { describe, expect, it } from "vitest";

import type { CodeHostProviderId } from "@/lib/codeHosts";

import { createPageLookupBudget } from "./lookupQueue";
import { detectMentions, mentionHref } from "./mentions";
import { pullRequestPageReferenceScope } from "./scope";

const written = (text: string) =>
  detectMentions(text).map(({ start, end }) => text.slice(start, end));

describe("detectMentions", () => {
  it("finds people and teams in prose, with where each is", () => {
    expect(detectMentions("cc @octo-cat and @acme/web-core.")).toEqual([
      { login: "octo-cat", start: 3, end: 12 },
      { login: "acme/web-core", start: 17, end: 31 },
    ]);
    expect(written("(@octo), @octo: @octo!")).toEqual(["@octo", "@octo", "@octo"]);
  });

  it("leaves email addresses, paths, packages and URLs as written", () => {
    expect(written("mail octo@example.com")).toEqual([]);
    expect(written("see https://example.com/@octo and ./@octo")).toEqual([]);
    expect(written("install @types/node/foo")).toEqual([]);
    expect(written("@@octo @-octo @octo- @octo.js")).toEqual([]);
  });

  it("finds nothing without an `@`", () => {
    expect(detectMentions("plain text")).toEqual([]);
  });
});

describe("mentionHref", () => {
  it("links a person to their profile and a team to its organization's page", () => {
    expect(mentionHref("github.com", "octo")).toBe("https://github.com/octo");
    expect(mentionHref("git.example.com", "acme/web")).toBe(
      "https://git.example.com/orgs/acme/teams/web",
    );
  });
});

describe("a PR page's mentions", () => {
  const budget = createPageLookupBudget();
  const scope = (provider: CodeHostProviderId, host: string) =>
    pullRequestPageReferenceScope({ provider, host, repository: "acme/app" }, budget);

  it("link on the PR's own GitHub host, an Enterprise one too", () => {
    expect(scope("github", "git.example.com").mention?.("octo")).toBe(
      "https://git.example.com/octo",
    );
  });

  it("stay as written on other code hosts", () => {
    expect(scope("named-host", "devices.example.com").mention?.("octo")).toBeNull();
  });
});
