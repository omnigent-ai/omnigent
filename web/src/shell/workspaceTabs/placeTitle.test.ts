import { describe, expect, it } from "vitest";

import { distinctFileLabel, placeTitle, pullRequestUrlParts, truncateMiddle } from "./placeTitle";
import type { WorkspacePlace } from "./workspacePlace";

const PR = "https://github.com/o/r/pull/123";

describe("placeTitle", () => {
  it.each<
    [WorkspacePlace, Parameters<typeof placeTitle>[1], Partial<ReturnType<typeof placeTitle>>]
  >([
    [{ kind: "new" }, {}, { label: "New tab", icon: "new" }],
    [{ kind: "new", app: "terminal" }, {}, { label: "New shell" }],
    [
      { kind: "files" },
      { workspaceRoot: "/repo" },
      { label: "Files", tooltip: "/repo", icon: "files" },
    ],
    [
      { kind: "files", path: "src/app/main.go", line: { start: 3 } },
      {},
      { label: "main.go", tooltip: "src/app/main.go", icon: "file", truncate: "middle" },
    ],
    [
      { kind: "changes" },
      { changedFiles: ["a.go", "b.go"], changesBaseLabel: "main" },
      { label: "Diffs", badge: "2", tooltip: "Changes vs main", icon: "changes" },
    ],
    [
      { kind: "changes" },
      { changedFiles: [] },
      { label: "Diffs", tooltip: "Changes vs the branch point" },
    ],
    // The path says which file Changes review reveals; the tab keeps its name and count.
    [
      { kind: "changes", path: "cmd/main.go" },
      { changedFiles: ["a.go", "cmd/main.go", "c.go"], changesBaseLabel: "main" },
      { label: "Diffs", badge: "3", tooltip: "Changes vs main", icon: "changes" },
    ],
    [
      { kind: "changes", range: { base: "main" }, path: "a.go" },
      {},
      { label: "a.go", tooltip: "main…working copy · a.go" },
    ],
    // A tab opened for one changed file is named for it, as a pull request's file tab is.
    [
      { kind: "changes", path: "cmd/main.go", fileTab: true },
      { changedFiles: ["a.go", "cmd/main.go", "c.go"], changesBaseLabel: "main" },
      { label: "main.go", badge: "2/3", tooltip: "Changes vs main · cmd/main.go" },
    ],
    [
      { kind: "changes", range: { base: "main", head: "HEAD" } },
      {},
      { label: "main…HEAD", tooltip: "main…HEAD" },
    ],
    [
      { kind: "changes", range: { base: "main" }, path: "a.go" },
      {},
      { label: "a.go", tooltip: "main…working copy · a.go" },
    ],
    [{ kind: "pulls" }, {}, { label: "Pull requests", icon: "pulls" }],
    [
      { kind: "pull", url: PR, view: "summary" },
      { pullRequest: { title: "Fix login", number: 123 } },
      {
        label: "#123 Fix login",
        tooltip: "github.com/o/r #123 · Fix login · Overview",
        icon: "pull",
        truncate: "end",
      },
    ],
    [
      { kind: "pull", url: PR, view: "changes", path: "src/main.go" },
      { pullRequest: { title: "Fix login" } },
      {
        label: "#123 main.go",
        tooltip: "github.com/o/r #123 · Fix login · Changes · src/main.go",
        truncate: "middle",
      },
    ],
    [
      { kind: "pull", url: PR, view: "summary", focus: "checks" },
      {},
      { label: "Pull request #123", tooltip: "github.com/o/r #123 · Checks" },
    ],
    [
      { kind: "pull", url: "https://dev.azure.com/o/p/_git/r/pullrequest/9", view: "commits" },
      { pullRequest: { title: "Bump", numberPrefix: "!", identity: "Azure DevOps · o/r !9" } },
      { label: "!9 Bump", tooltip: "Azure DevOps · o/r !9 · Bump · Commits" },
    ],
    [
      { kind: "pull", url: "https://example.com/review", view: "summary" },
      {},
      { label: "Pull request", tooltip: "example.com/review Pull request · Overview" },
    ],
    [
      { kind: "terminal", key: "k" },
      { terminal: { label: "zsh · build", detail: "~/repo" } },
      { label: "zsh · build", tooltip: "zsh · build · ~/repo", icon: "terminal" },
    ],
    [{ kind: "terminal", key: "k" }, { connecting: true }, { label: "Shell", connecting: true }],
    [{ kind: "agents" }, {}, { label: "Agents" }],
    [{ kind: "tasks" }, {}, { label: "Tasks", icon: "tasks" }],
    [{ kind: "agent", id: "a" }, { agentName: "Reviewer" }, { label: "Reviewer", icon: "agent" }],
    [{ kind: "agent", id: "a" }, { agentName: "  " }, { label: "Sub-agent" }],
    [{ kind: "chat" }, {}, { label: "New side chat", icon: "chat" }],
    [{ kind: "chat", id: "c" }, { chatTitle: "Retry plan" }, { label: "Retry plan" }],
    [{ kind: "chat", id: "c" }, {}, { label: "Side chat" }],
    [{ kind: "search" }, {}, { label: "Search" }],
    [{ kind: "search", query: "TODO" }, {}, { label: "Search: TODO" }],
    [{ kind: "web" }, {}, { label: "New page", icon: "web" }],
    [
      { kind: "web" },
      { page: { url: "about:blank", title: "about:blank" } },
      { label: "New page", tooltip: "New page" },
    ],
    [{ kind: "web" }, { page: { url: "", title: "" } }, { label: "New page" }],
    [
      { kind: "web", url: "https://old.example/page" },
      { page: { url: "", title: "" } },
      { label: "New page", tooltip: "New page" },
    ],
    [
      { kind: "web", url: "https://old.example/page" },
      { page: { title: "Old page" } },
      { label: "Old page", tooltip: "https://old.example/page" },
    ],
    [
      { kind: "web", url: "https://example.com/" },
      { page: { url: "about:blank", title: "" } },
      { label: "New page" },
    ],
    [
      { kind: "web", url: "https://example.com/a" },
      { page: { title: "Example" } },
      { label: "Example", tooltip: "https://example.com/a" },
    ],
    [
      { kind: "web", url: "https://example.com/a" },
      { page: { url: "https://other.example/b", title: "" } },
      { label: "other.example", tooltip: "https://other.example/b" },
    ],
  ])("%o", (place, facts, expected) => {
    expect(placeTitle(place, facts)).toMatchObject(expected);
  });

  it("keeps a host's port, as the session's pull requests name it", () => {
    expect(pullRequestUrlParts("https://GHE.example:8443/two/b/pull/123")).toEqual({
      host: "ghe.example:8443",
      repository: "two/b",
    });
    expect(
      placeTitle(
        { kind: "pull", url: "https://ghe.example:8443/two/b/pull/123", view: "summary" },
        { pullRequest: { title: "Update dependencies", number: 123 } },
      ).tooltip,
    ).toBe("ghe.example:8443/two/b #123 · Update dependencies · Overview");
  });

  it("tells apart pull requests with the same number and title on different repositories or hosts", () => {
    const facts = { pullRequest: { title: "Update dependencies", number: 123 } };
    const tooltips = [
      "https://github.com/one/a/pull/123",
      "https://ghe.example/two/b/pull/123",
      "https://github.com/two/b/pull/123",
    ].map((url) => placeTitle({ kind: "pull", url, view: "summary" }, facts).tooltip);
    expect(tooltips).toEqual([
      "github.com/one/a #123 · Update dependencies · Overview",
      "ghe.example/two/b #123 · Update dependencies · Overview",
      "github.com/two/b #123 · Update dependencies · Overview",
    ]);
    expect(new Set(tooltips).size).toBe(3);
    const loaded = placeTitle(
      { kind: "pull", url: "https://ghe.example/two/b/pull/123", view: "summary" },
      { pullRequest: { ...facts.pullRequest, identity: "ghe.example · two/b #123" } },
    );
    expect(loaded.tooltip).toBe("ghe.example · two/b #123 · Update dependencies · Overview");
  });

  it("names GitLab merge requests by their project path before facts load", () => {
    expect(
      placeTitle(
        {
          kind: "pull",
          url: "https://gitlab.example.com/g/sub/p/-/merge_requests/5",
          view: "summary",
        },
        {
          pullRequest: { numberPrefix: "!" },
        },
      ).tooltip,
    ).toBe("gitlab.example.com/g/sub/p !5 · Overview");
  });

  it("disambiguates same-named open files", () => {
    expect(
      placeTitle(
        { kind: "files", path: "cmd/server/BUILD.bazel" },
        { otherFilePaths: ["cmd/client/BUILD.bazel"] },
      ).label,
    ).toBe("BUILD.bazel · server");
  });
});

describe("distinctFileLabel", () => {
  it.each([
    ["a/b.ts", [], "b.ts"],
    ["a/b.ts", ["a/b.ts"], "b.ts"],
    ["x/server/BUILD", ["y/client/BUILD"], "BUILD · server"],
    ["x/server/BUILD", ["y/server/BUILD"], "BUILD · x/server"],
    ["x/server/BUILD", ["y/server/BUILD", "z/client/BUILD"], "BUILD · x/server"],
    ["BUILD", ["a/BUILD"], "BUILD"],
    ["/abs/one/main.go", ["/abs/two/main.go"], "main.go · one"],
    ["a/main.go", ["b/a/main.go"], "a/main.go"],
  ])("%s among %o → %s", (path, others, expected) => {
    expect(distinctFileLabel(path, others)).toBe(expected);
  });
});

describe("truncateMiddle", () => {
  it.each([
    ["short.ts", 20, "short.ts"],
    ["very_long_module_name_test.go", 15, "very_lo…test.go"],
    ["abcdef", 1, "…"],
    ["abcdef", 0, "…"],
    ["😀😀😀😀😀😀", 5, "😀😀…😀😀"],
  ])("%s at %d → %s", (text, max, expected) => {
    expect(truncateMiddle(text, max)).toBe(expected);
  });
});
