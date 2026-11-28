import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";

import {
  type AttachPullRequestContext,
  CURRENT_BRANCH_GROUP,
  OTHER_SESSIONS_GROUP,
  attachPullRequestFromInput,
  attachPullRequestHint,
  attachPullRequestSuggestions,
  pullRequestShortLabel,
  workspaceRemoteEvidence,
} from "./attachPullRequestItems";
import { EXAMPLE_DEVICE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// Example Host stand-in hosts are recognized by name, as a build that registers Example Host does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_DEVICE_HOST] });

function session(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return { id, title: `Session ${id}`, updated_at: 0, ...overrides } as Conversation;
}

function linkedSession(id: string, number: number, repository = "acme/app"): Conversation {
  return session(id, {
    pull_request: {
      url: `https://github.com/${repository}/pull/${number}`,
      provider: "github",
      repository,
      number,
      title: `Fix ${number}`,
      state: "OPEN",
    },
  });
}

const context: AttachPullRequestContext = {
  sessionId: "current",
  provider: "github",
  repository: "acme/app",
  numberPrefix: "#",
  requestName: "pull request",
  sessionPullRequests: [],
  accountHosts: ["github.com"],
  linkedUrls: ["https://github.com/acme/app/pull/1"],
  branchPullRequest: {
    url: "https://github.com/acme/app/pull/2",
    number: 2,
    title: "Branch work",
    state: "OPEN",
    is_draft: true,
  },
  conversations: [
    linkedSession("current", 9),
    linkedSession("a", 3),
    linkedSession("b", 1),
    linkedSession("c", 2),
    linkedSession("d", 4, "acme/other"),
    { ...linkedSession("e", 5), archived: true },
    linkedSession("f", 6),
  ],
};

describe("attachPullRequestSuggestions", () => {
  it("suggests the branch PR, then PRs from other sessions in this repository", () => {
    const items = attachPullRequestSuggestions(context);
    expect(items.map((item) => [item.group, item.value])).toEqual([
      [CURRENT_BRANCH_GROUP, "https://github.com/acme/app/pull/2"],
      [OTHER_SESSIONS_GROUP, "https://github.com/acme/app/pull/3"],
      [OTHER_SESSIONS_GROUP, "https://github.com/acme/app/pull/6"],
    ]);
    expect(items[0]).toMatchObject({
      label: "Branch work",
      description: "acme/app#2",
      detail: "Draft",
      keywords: ["2", "https://github.com/acme/app/pull/2"],
    });
    expect(items[1].description).toBe("acme/app#3 · Session a");
  });

  it("suggests every repository's PRs when the session's repository is unknown", () => {
    const items = attachPullRequestSuggestions({
      ...context,
      repository: null,
      branchPullRequest: null,
    });
    expect(items.map((item) => item.value)).toContain("https://github.com/acme/other/pull/4");
  });

  it("leaves out the same owner/repo on another host once the session's host is known", () => {
    const items = attachPullRequestSuggestions({
      ...context,
      branchPullRequest: null,
      sessionPullRequests: [
        { url: "https://github.com/acme/app/pull/1", host: "github.com", repository: "acme/app" },
      ],
      conversations: [
        linkedSession("github", 3),
        session("enterprise", {
          pull_request: {
            url: "https://ghe.example.com/acme/app/pull/8",
            provider: "github",
            repository: "acme/app",
            number: 8,
          },
        }),
      ],
    });
    expect(items.map((item) => item.value)).toEqual(["https://github.com/acme/app/pull/3"]);
  });

  it("caps suggestions from other sessions", () => {
    const conversations = Array.from({ length: 30 }, (_, index) =>
      linkedSession(`s${index}`, 100 + index),
    );
    const items = attachPullRequestSuggestions({
      ...context,
      branchPullRequest: null,
      conversations,
    });
    expect(items).toHaveLength(20);
  });
});

describe("workspaceRemoteEvidence", () => {
  const ok = {
    status: "ok" as const,
    context_key: "key",
    branch: "feature",
    head_sha: null,
    push: null,
    push_unresolved: null,
    remotes: [{ name: "origin", host: "ghe.example.com", owner: "acme", repo: "app" }],
    unsupported_remote_count: 0,
    truncated_remotes: true,
  };

  it("is reading until a first answer, and while the helper is still starting", () => {
    expect(workspaceRemoteEvidence({ data: undefined, isFetching: true }, "acme/app")).toEqual({
      status: "reading",
    });
    expect(
      workspaceRemoteEvidence({ data: { status: "pending" }, isFetching: false }, "acme/app"),
    ).toEqual({ status: "reading" });
    // A new read may name another host than the cached answer.
    expect(workspaceRemoteEvidence({ data: ok, isFetching: true }, "acme/app")).toEqual({
      status: "reading",
    });
    expect(
      workspaceRemoteEvidence({ data: { status: "failed" }, isFetching: true }, "acme/app"),
    ).toEqual({ status: "reading" });
  });

  it("carries the remote's hosts and whether the list was complete", () => {
    expect(workspaceRemoteEvidence({ data: ok, isFetching: false }, "acme/app")).toEqual({
      status: "read",
      hosts: ["ghe.example.com"],
      complete: false,
    });
  });

  it("is absent where the remotes can't be read, so the other sources decide", () => {
    for (const status of ["unsupported", "not_a_git_repo", "failed"] as const) {
      expect(workspaceRemoteEvidence({ data: { status }, isFetching: false }, "acme/app")).toBe(
        undefined,
      );
    }
    expect(workspaceRemoteEvidence({ data: undefined, isFetching: false }, "acme/app")).toBe(
      undefined,
    );
  });
});

describe("attachPullRequestHint", () => {
  const github = "https://github.com/owner/repo/pull/123";

  it("shows this repository's own link where a number resolves", () => {
    expect(attachPullRequestHint({ ...context, accountHosts: ["ghe.example.com"] }, github)).toBe(
      "Paste a link like https://ghe.example.com/acme/app/pull/123, or type #123.",
    );
  });

  it("falls back to the provider's example, or to none where links vary", () => {
    expect(attachPullRequestHint({ ...context, accountHosts: [] }, github)).toBe(
      `Paste a link like ${github}, or type #123.`,
    );
    expect(
      attachPullRequestHint(
        { ...context, provider: "azure_devops", numberPrefix: "!", accountHosts: [] },
        null,
      ),
    ).toBe("Paste a link, or type !123.");
  });
});

describe("attachPullRequestFromInput", () => {
  it("offers a typed number as a link in the session's repository", () => {
    expect(attachPullRequestFromInput("#42", context)).toEqual({
      id: "https://github.com/acme/app/pull/42",
      label: "Link acme/app#42",
      description: "https://github.com/acme/app/pull/42",
      rememberInHistory: false,
      value: "https://github.com/acme/app/pull/42",
    });
  });

  it("names a typed link's PR as this Mac looked it up, and only that PR", () => {
    const preview = {
      url: "https://github.com/acme/app/pull/42",
      number: 42,
      title: "Faster pull requests",
      state: "OPEN",
      is_draft: true,
      repository: "acme/app",
      updated_at: null,
    };
    expect(attachPullRequestFromInput("#42", { ...context, preview })).toEqual({
      id: "https://github.com/acme/app/pull/42",
      label: "Link “Faster pull requests”",
      description: "acme/app#42",
      detail: "Draft",
      rememberInHistory: false,
      value: "https://github.com/acme/app/pull/42",
    });
    expect(attachPullRequestFromInput("#43", { ...context, preview })).toMatchObject({
      label: "Link acme/app#43",
    });
  });

  it("uses the suggestion's ID so a matching suggestion isn't listed twice", () => {
    const typed = attachPullRequestFromInput("https://github.com/acme/app/pull/2/files", context);
    const [branch] = attachPullRequestSuggestions(context);
    expect(typed).toMatchObject({ id: branch.id });
  });

  it("explains invalid input and PRs that are already linked", () => {
    expect(attachPullRequestFromInput("https://github.com/acme/app/issues/1", context)).toEqual({
      error: expect.stringContaining("isn’t a GitHub pull request"),
    });
    expect(attachPullRequestFromInput("#1", context)).toEqual({
      error: "That pull request is already linked to this session.",
    });
  });

  it("leaves search text to filter suggestions", () => {
    expect(attachPullRequestFromInput("branch", context)).toBeNull();
  });
});

describe("pullRequestShortLabel", () => {
  it("names a PR by repository and number, or falls back to the link", () => {
    expect(pullRequestShortLabel("https://github.com/acme/app/pull/7", "#")).toBe("acme/app#7");
    expect(pullRequestShortLabel("https://example.test/review", "#")).toBe(
      "https://example.test/review",
    );
  });
});
