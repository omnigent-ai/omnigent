import { describe, expect, it } from "vitest";

import { groupClosesOf, TAB_GROUPS, tabGroupOf, tabPullRequestUrl } from "./tabGroups";
import type { WorkspaceTab } from "./workspaceTab";

const tab = (place: WorkspaceTab["place"], binding?: WorkspaceTab["binding"]): WorkspaceTab => ({
  id: "t",
  place,
  ...(binding ? { binding } : {}),
  openedBy: "user",
});
const PR = "https://github.example.com/acme/repo/pull/12";

describe("tabGroupOf", () => {
  it.each([
    [tab({ kind: "pull", url: PR, view: "summary" }), "pull-requests"],
    [tab({ kind: "pull", url: PR, view: "changes", path: "a.ts" }), "pull-requests"],
    [tab({ kind: "files", path: "a.ts" }), "files"],
    [tab({ kind: "changes" }), "files"],
    [tab({ kind: "terminal", key: "k" }, { kind: "terminal", key: "k" }), "shells"],
    [tab({ kind: "web", url: "https://example.com" }), "pages"],
    [tab({ kind: "new" }), "other"],
    [tab({ kind: "pulls" }), "other"],
    [tab({ kind: "agents" }), "other"],
  ] as const)("lists %o under %s", (given, group) => {
    expect(tabGroupOf(given)).toBe(group);
  });

  it("lists the groups in order, with Close all for all but shells and other tabs", () => {
    expect(TAB_GROUPS.map((group) => [group.label, group.closeAll ?? null])).toEqual([
      ["Pull requests", "close-pull-request-tabs"],
      ["Files", "close-file-tabs"],
      ["Shells", null],
      ["Pages", "close-page-tabs"],
      ["Other", null],
    ]);
  });
});

describe("groupClosesOf", () => {
  it("takes a merged PR's tab in Close merged as well as Close all", () => {
    expect(groupClosesOf("pull-requests", true)).toEqual([
      "close-merged-pull-requests",
      "close-pull-request-tabs",
    ]);
    expect(groupClosesOf("pull-requests", false)).toEqual(["close-pull-request-tabs"]);
    expect(groupClosesOf("files", true)).toEqual(["close-file-tabs"]);
  });

  it("never takes a shell", () => {
    expect(groupClosesOf("shells", false)).toEqual([]);
  });
});

describe("tabPullRequestUrl", () => {
  it("names the pull request of a PR tab and of its file tabs only", () => {
    expect(tabPullRequestUrl(tab({ kind: "pull", url: PR, view: "summary" }))).toBe(PR);
    expect(tabPullRequestUrl(tab({ kind: "pull", url: PR, view: "changes", path: "a.ts" }))).toBe(
      PR,
    );
    expect(tabPullRequestUrl(tab({ kind: "pulls" }))).toBeUndefined();
  });
});
