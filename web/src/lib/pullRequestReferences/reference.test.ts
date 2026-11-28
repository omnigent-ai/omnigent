import { describe, expect, it } from "vitest";

import {
  type PullRequestReferenceContext,
  referenceContextFor,
  referenceFromLinkOnAnyHost,
  referenceFromShorthand,
  referenceFromUrl,
  sessionReferenceContext,
} from "./reference";

const page = referenceContextFor("https://github.com/acme/app/pull/40")!;

describe("referenceContextFor", () => {
  it("reads the page's own PR, and nothing for a page that isn't one", () => {
    expect(page).toEqual({
      provider: "github",
      host: "github.com",
      repository: "acme/app",
    });
    expect(referenceContextFor("https://example.com/docs")).toBeNull();
    expect(referenceContextFor(null)).toBeNull();
  });
});

describe("referenceFromShorthand", () => {
  it.each([
    ["#12", "https://github.com/acme/app/pull/12", "#12"],
    ["site#3", "https://github.com/acme/site/pull/3", "acme/site#3"],
    ["other/lib#9", "https://github.com/other/lib/pull/9", "other/lib#9"],
    ["ACME/APP#5", "https://github.com/ACME/APP/pull/5", "#5"],
  ])("reads %s against the page's repository", (written, url, label) => {
    const reference = referenceFromShorthand(written, page);
    expect(reference?.target.url).toBe(url);
    expect(reference?.label).toBe(label);
  });

  it("names nothing for text that isn't a whole reference", () => {
    expect(referenceFromShorthand("see #12", page)).toBeNull();
    expect(referenceFromShorthand("C#7", page)).toBeNull();
  });

  it("stays on a GitHub Enterprise page's own host", () => {
    const enterprise: PullRequestReferenceContext = { ...page, host: "git.example.com" };
    expect(referenceFromShorthand("#12", enterprise)?.target).toMatchObject({
      provider: "github",
      host: "git.example.com",
      url: "https://git.example.com/acme/app/pull/12",
    });
  });
});

describe("referenceFromUrl", () => {
  it("reads a PR link, whatever follows its number", () => {
    for (const href of [
      "https://github.com/acme/app/pull/12",
      "https://github.com/acme/app/pull/12/files",
      "https://github.com/acme/app/pull/12#discussion_r1",
    ]) {
      expect(referenceFromUrl(href, page)).toMatchObject({
        target: { url: "https://github.com/acme/app/pull/12" },
        label: "#12",
      });
    }
    expect(referenceFromUrl("https://github.com/other/lib/pull/3", page)?.label).toBe(
      "other/lib#3",
    );
  });

  it("leaves a PR on another host alone, as an Enterprise page's link to github.com", () => {
    const enterprise = referenceContextFor("https://git.example.com/acme/app/pull/40");
    const onEnterprise: PullRequestReferenceContext = enterprise ?? {
      ...page,
      host: "git.example.com",
    };
    expect(referenceFromUrl("https://github.com/acme/app/pull/12", onEnterprise)).toBeNull();
    expect(referenceFromUrl("https://git.example.com/acme/app/pull/12", onEnterprise)?.label).toBe(
      "#12",
    );
  });

  it("leaves other links alone", () => {
    expect(referenceFromUrl("https://github.com/acme/app/issues/12", page)).toBeNull();
    expect(referenceFromUrl("https://example.com/acme/app/pull/12", page)).toBeNull();
    expect(referenceFromUrl("#section", page)).toBeNull();
  });
});

describe("sessionReferenceContext", () => {
  const onGitHub = { url: "https://github.com/acme/app/pull/7", host: "github.com" };
  const onEnterprise = {
    url: "https://git.example.com/acme/app/pull/8",
    host: "git.example.com",
    provider: "github",
  };

  it("puts the session's repository on the host its own PR is on, ignoring case", () => {
    expect(sessionReferenceContext("ACME/App", [onGitHub])).toEqual({
      provider: "github",
      host: "github.com",
      repository: "acme/app",
    });
    expect(sessionReferenceContext("acme/app", [onEnterprise])?.host).toBe("git.example.com");
  });

  it("takes its branch's PR alone, and a second PR on the same host changes nothing", () => {
    const sameHost = { url: "https://github.com/acme/app/pull/9", host: "github.com" };
    expect(sessionReferenceContext("acme/app", [onGitHub, sameHost])?.host).toBe("github.com");
  });

  it.each([
    ["no PR names a host, whatever this Mac reads", "acme/app", []],
    ["its repository's name is on two hosts", "acme/app", [onGitHub, onEnterprise]],
    [
      "only other repositories' PRs are linked",
      "acme/app",
      [{ ...onGitHub, url: "https://github.com/acme/lib/pull/2" }],
    ],
    ["its remote names no repository", null, [onGitHub]],
    ["its remote names an empty one", "  ", [onGitHub]],
    [
      "a linked PR's URL isn't a PR",
      "acme/app",
      [{ url: "https://example.com/x", host: "example.com" }],
    ],
  ])("names nothing when %s", (_case, repository, pullRequests) => {
    expect(sessionReferenceContext(repository, pullRequests)).toBeNull();
  });
});

describe("referenceFromLinkOnAnyHost", () => {
  const session = sessionReferenceContext("acme/app", [
    { url: "https://github.com/acme/app/pull/7", host: "github.com" },
  ]);

  it("reads a link on its own host, labelled short only in the session's own repository", () => {
    expect(
      referenceFromLinkOnAnyHost("https://github.com/acme/app/pull/12/files", session),
    ).toEqual({
      target: expect.objectContaining({ url: "https://github.com/acme/app/pull/12" }),
      label: "#12",
    });
    expect(referenceFromLinkOnAnyHost("https://github.com/other/lib/pull/3", session)?.label).toBe(
      "other/lib#3",
    );
  });

  it("labels every link in full where the session's repository is unknown", () => {
    expect(referenceFromLinkOnAnyHost("https://github.com/acme/app/pull/12", null)?.label).toBe(
      "acme/app#12",
    );
  });

  it("leaves a link that isn't a PR, or is on a host nothing reads, as written", () => {
    expect(referenceFromLinkOnAnyHost("https://example.com/acme/app/pull/1", session)).toBeNull();
    expect(referenceFromLinkOnAnyHost("https://github.com/acme/app/issues/1", session)).toBeNull();
    expect(referenceFromLinkOnAnyHost(undefined, session)).toBeNull();
  });
});
