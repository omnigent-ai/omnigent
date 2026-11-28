import { describe, expect, it } from "vitest";

import {
  AVAILABLE,
  isCommandAvailable,
  isCommandListed,
  unavailableBecause,
} from "@/store/paletteCommands";
import { EXAMPLE_REVIEW_SITE } from "@/test/pullRequestIntegrations";

import {
  type PullRequestCommandFacts,
  pullRequestCommands,
  pullRequestCommandsById,
  pullRequestPaletteName,
  type PullRequestTarget,
  REFRESHING_PULL_REQUEST_REASON,
  REMOVAL_PENDING_REASON,
  removeFinishedPullRequestsCommand,
} from "./model";

const GITHUB_PR: PullRequestTarget = {
  url: "https://github.com/acme/app/pull/12",
  number: 12,
  title: "Fix the build",
};

const ROW: PullRequestCommandFacts = {
  openable: true,
  reviewSite: null,
  removal: "available",
};

function listed(target: PullRequestTarget, facts: PullRequestCommandFacts) {
  return pullRequestCommands(target, facts).filter((command) =>
    isCommandListed(command.availability),
  );
}

describe("pullRequestCommands", () => {
  it("lists the row menu's sections in order", () => {
    expect(
      listed(GITHUB_PR, { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE }).map((command) => [
        command.section,
        command.menuLabel,
      ]),
    ).toEqual([
      ["open", "Open"],
      ["open", "Open on GitHub"],
      ["open", "Open in Example Review"],
      ["copy", "Copy GitHub link"],
      ["copy", "Copy Example Review link"],
      ["copy", "Copy pull request number"],
      ["copy", "Copy title"],
      ["remove", "Remove from this session"],
    ]);
  });

  it("names the PR in ⌘K", () => {
    const byId = pullRequestCommandsById(GITHUB_PR, { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE });
    expect(byId.open.paletteLabel).toBe("Open #12 Fix the build");
    expect(byId["open-in-review-site"].paletteLabel).toBe(
      "Open #12 Fix the build in Example Review",
    );
    expect(byId["copy-number"].paletteLabel).toBe("Copy pull request number of #12 Fix the build");
    expect(byId.remove.paletteLabel).toBe("Remove #12 Fix the build from this session");
  });

  it("opens and copies the exact page a link names, and reviews the PR itself", () => {
    const linkUrl = `${GITHUB_PR.url}/files#diff-1`;
    const byId = pullRequestCommandsById(
      { ...GITHUB_PR, linkUrl },
      { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE },
    );
    expect(byId["open-on-provider"].href).toBe(linkUrl);
    expect(byId["copy-link"].copy?.value).toBe(linkUrl);
    expect(byId["copy-review-site-link"].copy?.value).toBe(
      "https://review.example.com/acme/app/pull/12",
    );
  });

  it("copies the link, the Example Review link, the bare number and the title", () => {
    const byId = pullRequestCommandsById(GITHUB_PR, { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE });
    expect(byId["copy-link"].copy).toEqual({
      value: GITHUB_PR.url,
      copied: "GitHub link copied",
      failed: "Couldn’t copy the GitHub link",
    });
    expect(byId["copy-review-site-link"].copy?.value).toBe(
      "https://review.example.com/acme/app/pull/12",
    );
    expect(byId["open-in-review-site"].href).toBe(byId["copy-review-site-link"].copy?.value);
    expect(byId["copy-number"].copy?.value).toBe("12");
    expect(byId["copy-number"].copy?.copied).toBe("Pull request number copied");
    expect(byId["copy-title"].copy?.value).toBe("Fix the build");
  });

  it("hides the review site where none shows its links", () => {
    const ids = listed(GITHUB_PR, ROW).map((command) => command.id);
    expect(ids).not.toContain("open-in-review-site");
    expect(ids).not.toContain("copy-review-site-link");
  });

  it("offers the review site only for pull requests it has a page for", () => {
    const enterprise = { ...GITHUB_PR, url: "https://github.example.com/acme/app/pull/12" };
    const ids = listed(enterprise, { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE }).map(
      (command) => command.id,
    );
    expect(ids).not.toContain("open-in-review-site");
    expect(ids).not.toContain("copy-review-site-link");
    // Its own host's link stays.
    expect(ids).toContain("open-on-provider");
  });

  it("names another provider and its kind of request", () => {
    const gitlab: PullRequestTarget = {
      url: "https://gitlab.com/acme/app/-/merge_requests/5",
      number: 5,
      title: "Tidy",
      provider: "gitlab",
      providerDisplay: {
        id: "gitlab",
        display_name: "GitLab",
        request_name: "merge request",
        number_prefix: "!",
      },
    };
    const commands = listed(gitlab, { ...ROW, reviewSite: EXAMPLE_REVIEW_SITE });
    expect(commands.map((command) => command.menuLabel)).toEqual([
      "Open",
      "Open on GitLab",
      "Copy GitLab link",
      "Copy merge request number",
      "Copy title",
      "Remove from this session",
    ]);
    expect(pullRequestCommandsById(gitlab, ROW)["copy-number"].copy?.copied).toBe(
      "Merge request number copied",
    );
  });

  it("offers no title to copy before the PR's real title is known", () => {
    const loading = { ...GITHUB_PR, title: null };
    expect(listed(loading, ROW).map((command) => command.id)).not.toContain("copy-title");
    expect(pullRequestPaletteName(loading)).toBe("#12");
  });

  it("copies the head branch's name once the PR has been read, and hides it until then", () => {
    expect(
      isCommandListed(pullRequestCommandsById(GITHUB_PR, ROW)["copy-branch"].availability),
    ).toBe(false);
    const branch = pullRequestCommandsById({ ...GITHUB_PR, headRef: "ada/fix-build" }, ROW)[
      "copy-branch"
    ];
    expect(branch).toMatchObject({
      section: "copy",
      menuLabel: "Copy branch name",
      paletteLabel: "Copy branch name of #12 Fix the build",
      copy: {
        value: "ada/fix-build",
        copied: "Branch name copied",
        failed: "Couldn’t copy the branch name",
      },
    });
    expect(branch.keywords).toEqual(expect.arrayContaining(["branch", "checkout"]));
    // It follows the title among the copies.
    const ids = listed({ ...GITHUB_PR, headRef: "ada/fix-build" }, ROW).map(
      (command) => command.id,
    );
    expect(ids.indexOf("copy-branch")).toBe(ids.indexOf("copy-title") + 1);
  });

  it("hides Open where the details already show", () => {
    expect(listed(GITHUB_PR, { ...ROW, openable: false })[0].id).toBe("open-on-provider");
  });

  it("hides removal without access, and greys it while another removal runs", () => {
    expect(
      listed(GITHUB_PR, { ...ROW, removal: "hidden" }).map((command) => command.id),
    ).not.toContain("remove");
    const pending = pullRequestCommandsById(GITHUB_PR, { ...ROW, removal: "pending" }).remove;
    expect(isCommandListed(pending.availability)).toBe(true);
    expect(isCommandAvailable(pending.availability)).toBe(false);
    const counted = pullRequestCommandsById(GITHUB_PR, {
      ...ROW,
      removal: "pending",
      removalPendingReason: "Removing this pull request…",
    }).remove;
    expect(counted.availability).toEqual(unavailableBecause("Removing this pull request…"));
  });

  it("opens nothing for a link that isn't http", () => {
    const odd = { ...GITHUB_PR, url: "javascript:alert(1)" };
    expect(pullRequestCommandsById(odd, ROW)["open-on-provider"].availability).toEqual({
      available: false,
    });
  });
});

describe("the author's page", () => {
  it("has no command: the author's page isn't offered anywhere", () => {
    const commands = pullRequestCommandsById(GITHUB_PR, ROW);
    expect(Object.keys(commands)).not.toContain("open-author-profile");
    expect(Object.values(commands).filter((command) => /^Open @/.test(command.menuLabel))).toEqual(
      [],
    );
  });
});

describe("the link command", () => {
  it("stays out of surfaces that show the session's own PRs", () => {
    expect(pullRequestCommandsById(GITHUB_PR, ROW).link.availability).toEqual({
      available: false,
    });
    expect(listed(GITHUB_PR, ROW).map((command) => command.id)).not.toContain("link");
  });

  it("leads a chat link's menu, named for the session, with its reason when it can't run", () => {
    const chatLink: PullRequestTarget = { ...GITHUB_PR, title: null };
    const facts = { ...ROW, openable: false, removal: "hidden" as const };
    const [first] = listed(chatLink, { ...facts, link: AVAILABLE });
    expect(first).toMatchObject({
      id: "link",
      section: "link",
      menuLabel: "Link to this session",
      paletteLabel: "Link #12 to this session",
    });
    expect(first.keywords).toEqual(expect.arrayContaining(["link", "attach", "pull request"]));
    const blocked = pullRequestCommandsById(chatLink, {
      ...facts,
      link: unavailableBecause("Already linked to this session."),
    }).link;
    expect(isCommandListed(blocked.availability)).toBe(true);
    expect(isCommandAvailable(blocked.availability)).toBe(false);
  });
});

describe("refreshing one pull request", () => {
  it("is offered only by the PR's own details, first, and named in ⌘K", () => {
    // A row never offers it: its facts have no refresh.
    expect(listed(GITHUB_PR, ROW).map((command) => command.id)).not.toContain("refresh");
    const details = { ...ROW, openable: false, refresh: AVAILABLE };
    const [first] = listed(GITHUB_PR, details);
    expect(first).toMatchObject({
      id: "refresh",
      section: "refresh",
      menuLabel: "Refresh Pull Request",
      paletteLabel: "Refresh #12 Fix the build",
    });
  });

  it("waits while a refresh runs, saying so", () => {
    const byId = pullRequestCommandsById(GITHUB_PR, {
      ...ROW,
      refresh: unavailableBecause(REFRESHING_PULL_REQUEST_REASON),
    });
    expect(isCommandAvailable(byId.refresh.availability)).toBe(false);
    expect(isCommandListed(byId.refresh.availability)).toBe(true);
  });
});

describe("removing the session's merged and closed pull requests", () => {
  it("says how many in ⌘K, and is available only with some to remove and access to remove", () => {
    const some = removeFinishedPullRequestsCommand({ finished: 2, removal: "available" });
    expect(some.menuLabel).toBe("Remove Closed Pull Requests");
    expect(some.paletteLabel).toBe("Remove merged and closed pull requests (2)");
    expect(isCommandAvailable(some.availability)).toBe(true);

    const none = removeFinishedPullRequestsCommand({ finished: 0, removal: "available" });
    expect(isCommandAvailable(none.availability)).toBe(false);
    expect(isCommandListed(none.availability)).toBe(true);

    const pending = removeFinishedPullRequestsCommand({ finished: 2, removal: "pending" });
    expect(pending.availability).toEqual(unavailableBecause(REMOVAL_PENDING_REASON));
    const counted = removeFinishedPullRequestsCommand({
      finished: 2,
      removal: "pending",
      removalPendingReason: "Removing 1 of 2 pull requests…",
    });
    expect(counted.availability).toEqual(unavailableBecause("Removing 1 of 2 pull requests…"));

    const hidden = removeFinishedPullRequestsCommand({ finished: 2, removal: "hidden" });
    expect(isCommandListed(hidden.availability)).toBe(false);
  });
});
