import { describe, expect, it } from "vitest";

import {
  canonicalPullRequestUrl,
  parsePullRequestReference,
  pullRequestIn,
  pullRequestKey,
  type PullRequestReferenceContext,
  type SessionPullRequestIdentity,
  type WorkspaceRemoteEvidence,
} from "./pullRequestReference";

const github: PullRequestReferenceContext = {
  provider: "github",
  repository: "acme/app",
  numberPrefix: "#",
  sessionPullRequests: [],
  accountHosts: ["github.com"],
};

/** The workspace's remotes, read, naming the repository on `hosts`. */
function remote(...hosts: string[]): WorkspaceRemoteEvidence {
  return { status: "read", hosts, complete: true };
}

function sessionPr(
  url: string,
  repository: string,
  provider: string | undefined = "github",
): SessionPullRequestIdentity {
  return { url, host: new URL(url).host, repository, provider };
}

describe("parsePullRequestReference: numbers", () => {
  it("ignores empty input and plain search text", () => {
    expect(parsePullRequestReference("   ", github)).toBeNull();
    expect(parsePullRequestReference("fix login", github)).toBeNull();
  });

  it("resolves #123 and 123 on the signed-in GitHub host", () => {
    const expected = {
      kind: "link",
      url: "https://github.com/acme/app/pull/123",
      repository: "acme/app",
      number: 123,
    };
    expect(parsePullRequestReference("#123", github)).toEqual(expected);
    expect(parsePullRequestReference(" 123 ", github)).toEqual(expected);
    expect(parsePullRequestReference("#9", { ...github, provider: undefined })).toMatchObject({
      url: "https://github.com/acme/app/pull/9",
    });
  });

  it("prefers the host of this session's own PRs in the repository", () => {
    const reference = parsePullRequestReference("#7", {
      ...github,
      accountHosts: ["github.com", "ghe.example.com"],
      sessionPullRequests: [sessionPr("https://GHE.example.com/acme/app/pull/1", "acme/app")],
    });
    expect(reference).toMatchObject({ url: "https://ghe.example.com/acme/app/pull/7" });
  });

  it("requires a known repository instead of borrowing another one", () => {
    expect(
      parsePullRequestReference("#42", {
        ...github,
        repository: null,
        sessionPullRequests: [sessionPr("https://github.com/other/repo/pull/2", "other/repo")],
      }),
    ).toEqual({
      kind: "invalid",
      error:
        "This session’s repository isn’t known yet, so #42 can’t be found. Paste the full link instead.",
    });
  });

  it("ignores PRs from another provider when resolving a GitHub number", () => {
    const reference = parsePullRequestReference("#42", {
      ...github,
      accountHosts: [],
      sessionPullRequests: [
        sessionPr("https://gitlab.com/acme/app/-/merge_requests/2", "acme/app", "gitlab"),
      ],
    });
    expect(reference).toEqual({
      kind: "invalid",
      error:
        "Paste the full link, like https://github.com/acme/app/pull/42. It isn’t known yet which GitHub server hosts acme/app.",
    });
  });

  describe("the workspace's own remote", () => {
    // The session host reports the repository but no account when gh reaches it.
    const signedInHost = { ...github, accountHosts: [] };

    it("resolves a bare number on the remote's host when nothing else names one", () => {
      for (const input of ["250", "#250"]) {
        expect(
          parsePullRequestReference(input, {
            ...signedInHost,
            workspaceRemote: remote("github.com"),
          }),
        ).toEqual({
          kind: "link",
          url: "https://github.com/acme/app/pull/250",
          repository: "acme/app",
          number: 250,
        });
      }
      expect(
        parsePullRequestReference("#5", {
          ...signedInHost,
          workspaceRemote: remote("GHE.example.com"),
        }),
      ).toMatchObject({ url: "https://ghe.example.com/acme/app/pull/5" });
    });

    it("settles which of several signed-in hosts serves the repository", () => {
      expect(
        parsePullRequestReference("#5", {
          ...github,
          accountHosts: ["github.com", "ghe.example.com"],
          workspaceRemote: remote("ghe.example.com"),
        }),
      ).toMatchObject({ url: "https://ghe.example.com/acme/app/pull/5" });
    });

    it("yields to the host of the session's own PRs in the repository", () => {
      expect(
        parsePullRequestReference("#5", {
          ...signedInHost,
          workspaceRemote: remote("github.com"),
          sessionPullRequests: [sessionPr("https://ghe.example.com/acme/app/pull/1", "acme/app")],
        }),
      ).toMatchObject({ url: "https://ghe.example.com/acme/app/pull/5" });
    });

    it("waits for the remotes before using a lower-priority account, but not over the session's PRs", () => {
      const reading = { ...github, workspaceRemote: { status: "reading" } as const };
      expect(parsePullRequestReference("250", reading)).toEqual({ kind: "pending" });
      expect(
        parsePullRequestReference("250", {
          ...reading,
          sessionPullRequests: [sessionPr("https://ghe.example.com/acme/app/pull/1", "acme/app")],
        }),
      ).toMatchObject({ url: "https://ghe.example.com/acme/app/pull/250" });
      // A pasted link never waits.
      expect(
        parsePullRequestReference("https://github.com/acme/app/pull/3", reading),
      ).toMatchObject({ kind: "link" });
    });

    it("treats an incomplete remote list that names the repository as ambiguous", () => {
      const partial = (...hosts: string[]): WorkspaceRemoteEvidence => ({
        status: "read",
        hosts,
        complete: false,
      });
      expect(
        parsePullRequestReference("#5", { ...github, workspaceRemote: partial("ghe.example.com") }),
      ).toEqual({
        kind: "invalid",
        error: "Paste the full link: #5 could be on more than one host.",
      });
      // Naming none, it says nothing; the account still decides.
      expect(
        parsePullRequestReference("#5", { ...github, workspaceRemote: partial() }),
      ).toMatchObject({ url: "https://github.com/acme/app/pull/5" });
    });

    it("explains remotes on more than one host", () => {
      expect(
        parsePullRequestReference("#5", {
          ...signedInHost,
          workspaceRemote: remote("github.com", "ghe.example.com"),
        }),
      ).toEqual({
        kind: "invalid",
        error: "Paste the full link: #5 could be on more than one host.",
      });
    });
  });

  describe("another provider", () => {
    const other: PullRequestReferenceContext = {
      provider: "example-host",
      repository: "acme/app",
      numberPrefix: "#",
      accountHosts: [],
      sessionPullRequests: [],
    };

    it("reuses the link of the session's own PR in the repository", () => {
      expect(
        parsePullRequestReference("#250", {
          ...other,
          sessionPullRequests: [
            sessionPr("https://git.example.com/acme/app/pull/9", "acme/app", "example-host"),
          ],
        }),
      ).toMatchObject({ url: "https://git.example.com/acme/app/pull/250" });
    });

    it("still needs a link when no PR of the repository shows its format", () => {
      expect(parsePullRequestReference("#250", other)).toEqual({
        kind: "invalid",
        error: "Paste the full link: the link format for #250 isn’t known.",
      });
    });
  });

  it("never builds a link from a session PR that reports no host", () => {
    // Hosts that predate PR tracking report the branch's PR with an empty host.
    const legacy = { url: "https://github.com/acme/app/pull/1", host: "", repository: "acme/app" };
    expect(
      parsePullRequestReference("#42", {
        ...github,
        accountHosts: [],
        sessionPullRequests: [legacy],
      }),
    ).toEqual({
      kind: "invalid",
      error:
        "Paste the full link, like https://github.com/acme/app/pull/42. It isn’t known yet which GitHub server hosts acme/app.",
    });
    expect(
      parsePullRequestReference("#42", { ...github, accountHosts: ["", "github.com"] }),
    ).toMatchObject({ url: "https://github.com/acme/app/pull/42" });
  });

  it("explains when the same repository exists on more than one host", () => {
    const error = {
      kind: "invalid",
      error: "Paste the full link: #42 could be on more than one host.",
    };
    expect(
      parsePullRequestReference("#42", {
        ...github,
        sessionPullRequests: [
          sessionPr("https://github.com/acme/app/pull/1", "acme/app"),
          sessionPr("https://ghe.example.com/acme/app/pull/2", "acme/app"),
        ],
      }),
    ).toEqual(error);
    expect(
      parsePullRequestReference("#42", {
        ...github,
        accountHosts: ["github.com", "ghe.example.com"],
      }),
    ).toEqual(error);
  });

  describe("other providers", () => {
    const azure: PullRequestReferenceContext = {
      provider: "azure_devops",
      repository: "contoso/web/app",
      numberPrefix: "!",
      accountHosts: [],
      sessionPullRequests: [
        sessionPr(
          "https://dev.azure.com/contoso/web/_git/app/pullrequest/7",
          "contoso/web/app",
          "azure_devops",
        ),
      ],
    };

    it("reuses the shape of this repository's own link, matched by identity", () => {
      expect(parsePullRequestReference("!42", azure)).toEqual({
        kind: "link",
        url: "https://dev.azure.com/contoso/web/_git/app/pullrequest/42",
        repository: "contoso/web/app",
        number: 42,
      });
      expect(
        parsePullRequestReference("42", {
          ...azure,
          sessionPullRequests: [
            sessionPr(
              "https://contoso.visualstudio.com/web/_git/app/pullrequest/3",
              "contoso/web/app",
              "azure_devops",
            ),
          ],
        }),
      ).toMatchObject({ url: "https://contoso.visualstudio.com/web/_git/app/pullrequest/42" });
    });

    it("does not reuse a link from another repository", () => {
      expect(
        parsePullRequestReference("!42", {
          ...azure,
          sessionPullRequests: [
            sessionPr(
              "https://dev.azure.com/contoso/web/_git/other/pullrequest/7",
              "contoso/web/other",
              "azure_devops",
            ),
          ],
        }),
      ).toEqual({
        kind: "invalid",
        error: "Paste the full link: the link format for !42 isn’t known.",
      });
    });
  });
});

describe("parsePullRequestReference: numbers from the session's checkout", () => {
  const offline: PullRequestReferenceContext = {
    provider: null,
    repository: null,
    numberPrefix: "#",
    sessionPullRequests: [],
    accountHosts: [],
  };

  it("resolves on an Enterprise host the checkout names, with no host info", () => {
    expect(
      parsePullRequestReference("#12", {
        ...offline,
        contextRepositories: [
          { provider: "github", host: "ghe.example.com", repository: "team/app" },
        ],
      }),
    ).toEqual({
      kind: "link",
      url: "https://ghe.example.com/team/app/pull/12",
      repository: "team/app",
      number: 12,
    });
  });

  it("counts a repository the checkout names twice, in different case, once", () => {
    const host = "ghe.example.com";
    expect(
      parsePullRequestReference("34", {
        ...offline,
        contextRepositories: [
          { provider: "github", host, repository: "example-org/widgets" },
          { provider: "github", host, repository: "Example-Org/Widgets" },
        ],
      }),
    ).toMatchObject({ kind: "link", url: `https://${host}/example-org/widgets/pull/34` });
  });

  it("asks for the link when the checkout names a fork and its upstream, or two repositories", () => {
    const reference = parsePullRequestReference("#5", {
      ...offline,
      contextRepositories: [
        { provider: "github", host: "github.com", repository: "me/app" },
        { provider: "github", host: "github.com", repository: "acme/app" },
      ],
    });
    expect(reference).toEqual({
      kind: "invalid",
      error: "Paste the full link: #5 could be in me/app, acme/app.",
    });
  });

  it("waits while a host the checkout names, or its remotes, are still being checked", () => {
    expect(
      parsePullRequestReference("#5", {
        ...offline,
        contextRepositories: [
          { provider: "github", host: "github.com", repository: "acme/tools" },
          { provider: null, host: "ghe.acme.com", repository: "acme/internal" },
        ],
      }),
    ).toEqual({ kind: "pending" });
    expect(
      parsePullRequestReference("#5", { ...offline, workspaceRemote: { status: "reading" } }),
    ).toEqual({ kind: "pending" });
  });
});

describe("parsePullRequestReference: links", () => {
  it("normalizes GitHub links, including sub-pages and missing schemes", () => {
    expect(
      parsePullRequestReference("https://github.com/acme/web/pull/42/files?w=1#diff", github),
    ).toEqual({
      kind: "link",
      url: "https://github.com/acme/web/pull/42",
      repository: "acme/web",
      number: 42,
    });
    expect(parsePullRequestReference("github.com/acme/web/pull/42", github)).toMatchObject({
      url: "https://github.com/acme/web/pull/42",
    });
  });

  it("reads a pasted link by its own provider, not the session's", () => {
    expect(
      parsePullRequestReference("https://gitlab.com/acme/app/-/merge_requests/42", github),
    ).toEqual({
      kind: "link",
      url: "https://gitlab.com/acme/app/-/merge_requests/42",
      repository: null,
      number: null,
    });
    expect(
      parsePullRequestReference(
        "https://dev.azure.com/contoso/web/_git/app/pullrequest/9/",
        github,
      ),
    ).toMatchObject({ url: "https://dev.azure.com/contoso/web/_git/app/pullrequest/9" });
    expect(
      parsePullRequestReference("https://github.com/acme/app/pull/3", {
        ...github,
        provider: "gitlab",
      }),
    ).toMatchObject({ url: "https://github.com/acme/app/pull/3", number: 3 });
  });

  it("explains a non-PR link on a GitHub host, including GitHub Enterprise", () => {
    const error = { kind: "invalid", error: "That link isn’t a GitHub pull request." };
    expect(parsePullRequestReference("https://github.com/acme/web/issues/3", github)).toEqual(
      error,
    );
    expect(
      parsePullRequestReference("https://ghe.example.com/acme/web/issues/3", {
        ...github,
        accountHosts: ["ghe.example.com"],
      }),
    ).toEqual(error);
  });

  it("reads a GitHub-shaped pull request on any host, whatever the session's provider", () => {
    for (const host of ["git1.example.com", "git2.example.com"]) {
      const url = `https://${host}/example-org/widgets/pull/2346`;
      for (const context of [github, { ...github, provider: "gitlab", accountHosts: [] }]) {
        expect(parsePullRequestReference(url, context)).toEqual({
          kind: "link",
          url,
          repository: "example-org/widgets",
          number: 2346,
        });
      }
    }
  });

  it("rejects links that aren't web links or can't be parsed", () => {
    expect(parsePullRequestReference("ftp://github.com/acme/web/pull/3", github)).toEqual({
      kind: "invalid",
      error: "Paste a link that starts with https://.",
    });
    expect(parsePullRequestReference("https://exa mple.com/x", github)).toMatchObject({
      kind: "invalid",
    });
  });
});

describe("canonicalPullRequestUrl", () => {
  it("ignores host case, trailing slashes, query, and fragment", () => {
    expect(canonicalPullRequestUrl("https://GitHub.com/a/b/pull/1/?x=1#y")).toBe(
      "https://github.com/a/b/pull/1",
    );
  });
});

describe("pullRequestIn", () => {
  it("finds a list's PR however either link is written, and nothing for another PR", () => {
    const listed = new Map([["https://github.com/acme/app/pull/42", "row"]]);
    expect(pullRequestIn(listed, "https://github.com/acme/app/pull/42")).toBe("row");
    expect(pullRequestIn(listed, "https://GitHub.com/ACME/App/pull/42/")).toBe("row");
    expect(pullRequestIn(listed, "https://github.com/acme/app/pull/42?tab=files")).toBe("row");
    expect(pullRequestIn(listed, "https://github.com/acme/app/pull/43")).toBeUndefined();
    expect(pullRequestKey("https://GitHub.com/ACME/App/pull/42/")).toBe(
      "https://github.com/acme/app/pull/42",
    );
  });
});
