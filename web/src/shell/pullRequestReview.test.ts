import { describe, expect, it } from "vitest";
import { parsePatchFiles, type FileDiffMetadata } from "@pierre/diffs";
import type { PullRequestChangedFile } from "@/hooks/usePullRequests";
import {
  MATCH_LIMIT,
  buildSearchCorpus,
  filterChangedPaths,
  findMatches,
  firstMatchAtOrBelow,
  pullRequestFileUrl,
  pullRequestRepoUrl,
  refreshedMatchIndex,
  selectVisibleFiles,
  type Match,
  type PatchMatchTarget,
} from "./pullRequestReview";

function changed(path: string, previous_path?: string): PullRequestChangedFile {
  return {
    path,
    name: path.split("/").pop()!,
    previous_path,
    status: previous_path ? "renamed" : "modified",
    lines_added: 1,
    lines_removed: 0,
    bytes: null,
    modified_at: null,
  };
}

const FILES = [
  changed("src/shell/PullRequestPanel.tsx"),
  changed("src/lib/clipboard.ts", "src/utils/copy.ts"),
  changed("README.md"),
];
const paths = (files: PullRequestChangedFile[]) => files.map((file) => file.path);

describe("filterChangedPaths", () => {
  it.each([
    ["", FILES.map((file) => file.path)],
    ["   ", FILES.map((file) => file.path)],
    ["pullrequest", ["src/shell/PullRequestPanel.tsx"]],
    [" README ", ["README.md"]],
    ["src\\shell\\", ["src/shell/PullRequestPanel.tsx"]],
    ["src/", ["src/shell/PullRequestPanel.tsx", "src/lib/clipboard.ts"]],
    ["utils/copy", ["src/lib/clipboard.ts"]],
    ["missing", []],
  ])("matches %j", (query, expected) => {
    expect(paths(filterChangedPaths(FILES, query))).toEqual(expected);
  });
});

describe("selectVisibleFiles", () => {
  it("returns every file for empty inputs", () => {
    expect(selectVisibleFiles(FILES, { pathFilter: "", hiddenPaths: new Set() })).toEqual(FILES);
  });

  it("combines the path filter with hidden paths", () => {
    const visible = selectVisibleFiles(FILES, {
      pathFilter: "src",
      hiddenPaths: new Set(["src/lib/clipboard.ts", "README.md"]),
    });
    expect(paths(visible)).toEqual(["src/shell/PullRequestPanel.tsx"]);
  });
});

/** The parsed per-file diffs, keyed by path, as the Changes tab builds them. */
function parsed(patch: string): Map<string, FileDiffMetadata> {
  const diffs = new Map<string, FileDiffMetadata>();
  for (const part of parsePatchFiles(patch))
    for (const file of part.files) diffs.set(file.name, file);
  return diffs;
}

const TWO_HUNK_PATCH =
  "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n" +
  "@@ -1,1 +1,1 @@\n-a\n+b\n" +
  "@@ -20,2 +20,3 @@\n ctx\n-x\n+y\n+z\n";

function lineTargets(corpus: PatchMatchTarget[]) {
  return corpus
    .filter((target) => target.kind === "line")
    .map(({ side, line, text }) => ({ side, line, text }));
}

describe("buildSearchCorpus", () => {
  it("numbers context, deleted and added lines in the new and old file", () => {
    const patch =
      "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -10,3 +10,3 @@\n same\n-old\n+new\n tail \n";
    const corpus = buildSearchCorpus(parsed(patch), [changed("a.ts")]);
    expect(corpus[0]).toEqual({ path: "a.ts", kind: "path", text: "a.ts" });
    expect(lineTargets(corpus)).toEqual([
      { side: "additions", line: 10, text: "same" },
      { side: "deletions", line: 11, text: "old" },
      { side: "additions", line: 11, text: "new" },
      { side: "additions", line: 12, text: "tail " },
    ]);
  });

  it("keeps the line numbers of later hunks", () => {
    const corpus = buildSearchCorpus(parsed(TWO_HUNK_PATCH), [changed("a.ts")]);
    expect(lineTargets(corpus).slice(-4)).toEqual([
      { side: "additions", line: 20, text: "ctx" },
      { side: "deletions", line: 21, text: "x" },
      { side: "additions", line: 21, text: "y" },
      { side: "additions", line: 22, text: "z" },
    ]);
  });

  it("strips CRLF endings so text matches the rendered row", () => {
    const patch = "diff --git a/a.ts b/a.ts\n--- a/a.ts\n+++ b/a.ts\n@@ -1 +1 @@\n-x\r\n+new\r\n";
    const corpus = buildSearchCorpus(parsed(patch), [changed("a.ts")]);
    expect(lineTargets(corpus).map((target) => target.text)).toEqual(["x", "new"]);
  });

  it("adds a path target for the previous path of a rename", () => {
    const patch =
      "diff --git a/x.ts b/y.ts\nsimilarity index 100%\nrename from x.ts\nrename to y.ts\n";
    const corpus = buildSearchCorpus(parsed(patch), [changed("y.ts", "x.ts")]);
    expect(corpus).toEqual([
      { path: "y.ts", kind: "path", text: "y.ts" },
      { path: "y.ts", kind: "path", text: "x.ts" },
    ]);
  });

  it("falls back to the parsed previous name when the list lacks it", () => {
    const patch =
      "diff --git a/x.ts b/y.ts\nsimilarity index 100%\nrename from x.ts\nrename to y.ts\n";
    const corpus = buildSearchCorpus(parsed(patch), [changed("y.ts")]);
    expect(corpus.map((target) => target.text)).toEqual(["y.ts", "x.ts"]);
  });

  it("gives binary files only their path", () => {
    const patch =
      "diff --git a/img.png b/img.png\nindex 1..2 100644\nBinary files a/img.png and b/img.png differ\n";
    const corpus = buildSearchCorpus(parsed(patch), [changed("img.png")]);
    expect(corpus).toEqual([{ path: "img.png", kind: "path", text: "img.png" }]);
  });
});

describe("findMatches", () => {
  const CORPUS: PatchMatchTarget[] = [
    { path: "a.ts", kind: "line", side: "additions", line: 1, text: "Foo foo FOO" },
    { path: "b.ts", kind: "line", side: "deletions", line: 2, text: "a.c abc" },
    { path: "b.ts", kind: "path", text: "b.ts" },
  ];
  const options = { matchCase: false, regex: false, limit: MATCH_LIMIT };
  const spans = (matches: Match[]) =>
    matches.map((match) => [match.target.path, match.start, match.end]);

  it("finds every case-insensitive occurrence in corpus order", () => {
    const result = findMatches(CORPUS, "foo", options);
    expect(spans(result.matches)).toEqual([
      ["a.ts", 0, 3],
      ["a.ts", 4, 7],
      ["a.ts", 8, 11],
    ]);
    expect(result).toMatchObject({ truncated: false, invalid: false });
  });

  it("respects match case", () => {
    expect(spans(findMatches(CORPUS, "foo", { ...options, matchCase: true }).matches)).toEqual([
      ["a.ts", 4, 7],
    ]);
  });

  it("searches path targets too", () => {
    expect(spans(findMatches(CORPUS, "B.TS", options).matches)).toEqual([["b.ts", 0, 4]]);
  });

  it("treats plain queries literally", () => {
    expect(spans(findMatches(CORPUS, "a.c", options).matches)).toEqual([["b.ts", 0, 3]]);
  });

  it("applies regular expressions and skips empty matches", () => {
    expect(spans(findMatches(CORPUS, "^foo", { ...options, regex: true }).matches)).toEqual([
      ["a.ts", 0, 3],
    ]);
    expect(spans(findMatches(CORPUS, "a.c", { ...options, regex: true }).matches)).toEqual([
      ["b.ts", 0, 3],
      ["b.ts", 4, 7],
    ]);
    expect(findMatches(CORPUS, "x*", { ...options, regex: true }).matches).toEqual([]);
  });

  it("reports an invalid regular expression without matches", () => {
    expect(findMatches(CORPUS, "(", { ...options, regex: true })).toEqual({
      matches: [],
      truncated: false,
      invalid: true,
    });
  });

  it("returns nothing for a blank query", () => {
    expect(findMatches(CORPUS, "  ", options)).toEqual({
      matches: [],
      truncated: false,
      invalid: false,
    });
  });

  it("caps the matches and marks the result truncated", () => {
    const corpus: PatchMatchTarget[] = [
      { path: "a.ts", kind: "line", side: "additions", line: 1, text: "x x x" },
    ];
    expect(findMatches(corpus, "x", { ...options, limit: 2 })).toEqual({
      matches: expect.any(Array),
      truncated: true,
      invalid: false,
    });
    expect(findMatches(corpus, "x", { ...options, limit: 2 }).matches).toHaveLength(2);
    expect(findMatches(corpus, "x", { ...options, limit: 3 }).truncated).toBe(false);
  });
});

describe("match positions after a refresh or a reseed", () => {
  const match = (path: string, line: number | undefined, start = 0): Match => ({
    target:
      line === undefined
        ? { path, kind: "path", text: path }
        : { path, kind: "line", side: "additions", line, text: "x" },
    start,
    end: start + 1,
  });

  it("starts at the first match in a file at or below the viewport top", () => {
    const matches = [match("a.ts", 1), match("b.ts", 3), match("c.ts", 2)];
    expect(firstMatchAtOrBelow(matches, ["a.ts", "b.ts", "c.ts"], "b.ts")).toBe(1);
    expect(firstMatchAtOrBelow(matches, ["a.ts", "b.ts", "c.ts"], "d.ts")).toBe(0);
    expect(firstMatchAtOrBelow(matches, ["a.ts", "b.ts", "c.ts"], null)).toBe(0);
  });

  it("prefers the same side when falling back to the nearest line", () => {
    const deletion: Match = {
      target: { path: "a.ts", kind: "line", side: "deletions", line: 19, text: "x" },
      start: 0,
      end: 1,
    };
    const addition: Match = {
      target: { path: "a.ts", kind: "line", side: "additions", line: 40, text: "x" },
      start: 0,
      end: 1,
    };
    const previous: Match = {
      target: { path: "a.ts", kind: "line", side: "additions", line: 20, text: "gone" },
      start: 0,
      end: 1,
    };
    expect(refreshedMatchIndex([deletion, addition], previous)).toBe(1);
  });

  it("prefers the file's path match when the previous match was a path target", () => {
    const lineMatch: Match = {
      target: { path: "a.ts", kind: "line", side: "additions", line: 3, text: "x" },
      start: 0,
      end: 1,
    };
    const pathMatch: Match = {
      target: { path: "a.ts", kind: "path", text: "a.ts" },
      start: 0,
      end: 1,
    };
    const previous: Match = {
      target: { path: "a.ts", kind: "path", text: "old-name.ts" },
      start: 0,
      end: 1,
    };
    expect(refreshedMatchIndex([lineMatch, pathMatch], previous)).toBe(1);
  });

  it("tells the current and previous path of a rename apart", () => {
    const corpus: PatchMatchTarget[] = [
      { path: "y.ts", kind: "path", text: "y.ts" },
      { path: "y.ts", kind: "path", text: "x.ts" },
    ];
    const { matches } = findMatches(corpus, "ts", { matchCase: false, regex: false, limit: 10 });
    expect(refreshedMatchIndex(matches, matches[1])).toBe(1);
  });

  it("keeps the same match when it is still present", () => {
    const previous = match("a.ts", 5, 4);
    expect(refreshedMatchIndex([match("a.ts", 2), match("a.ts", 5, 4)], previous)).toBe(1);
  });

  it("moves to the nearest line in the same file when the match is gone", () => {
    const previous = match("a.ts", 10);
    const matches = [match("a.ts", 2), match("b.ts", 10), match("a.ts", 13)];
    expect(refreshedMatchIndex(matches, previous)).toBe(2);
  });

  it("falls back to the first match when the file has none", () => {
    expect(refreshedMatchIndex([match("b.ts", 1)], match("a.ts", 3))).toBe(0);
  });
});

describe("pullRequestRepoUrl", () => {
  it.each([
    ["github", "https://github.com/acme/app/pull/7", "https://github.com/acme/app"],
    ["github", "https://ghe.example.com/acme/app/pull/7", "https://ghe.example.com/acme/app"],
    // A repository named like the PR route marker must not cut the URL early.
    ["github", "https://github.com/pull/app/pull/7", "https://github.com/pull/app"],
    ["gitlab", "https://gitlab.com/acme/app/-/merge_requests/7", "https://gitlab.com/acme/app"],
    [
      "azure_devops",
      "https://dev.azure.com/acme/web/_git/app/pullrequest/7",
      "https://dev.azure.com/acme/web/_git/app",
    ],
    [
      "azure_devops",
      "https://dev.azure.com/acme/pullrequest/_git/pullrequest/pullrequest/7",
      "https://dev.azure.com/acme/pullrequest/_git/pullrequest",
    ],
    ["github", "https://github.com/acme/app/issues/7", null],
    ["github", "https://github.com/acme/pull/issues/7", null],
    ["github", "https://github.com/pull/123/pull/7", "https://github.com/pull/123"],
    [
      "azure_devops",
      "https://dev.azure.com/pullrequest/2024/_git/app/pullrequest/9",
      "https://dev.azure.com/pullrequest/2024/_git/app",
    ],
    ["github", "https://github.com/acme/app/pull/7/files", "https://github.com/acme/app"],
    ["unknown", "https://github.com/acme/app/pull/7", null],
    [null, "https://github.com/acme/app/pull/7", null],
  ])("derives the repository for %s from %s", (provider, prUrl, expected) => {
    expect(pullRequestRepoUrl(provider, prUrl)).toBe(expected);
  });
});

describe("pullRequestFileUrl", () => {
  const BASE_REPO = {
    github: "https://github.com/acme/app",
    gitlab: "https://gitlab.com/acme/app",
    azure_devops: "https://dev.azure.com/acme/web/_git/app",
  };
  const FORK_REPO = {
    github: "https://github.com/fork/app",
    gitlab: "https://gitlab.com/fork/app",
    azure_devops: "https://dev.azure.com/fork/web/_git/app",
  };

  // Spaces, `#` and non-ASCII are encoded per segment, and `/` stays.
  it.each([
    [
      "github",
      "https://github.com/fork/app/blob/head1/src/a%20b%23c/%C3%A9.ts",
      "https://github.com/acme/app/blob/head1/README.md",
    ],
    [
      "gitlab",
      "https://gitlab.com/fork/app/-/blob/head1/src/a%20b%23c/%C3%A9.ts",
      "https://gitlab.com/acme/app/-/blob/head1/README.md",
    ],
    [
      "azure_devops",
      "https://dev.azure.com/fork/web/_git/app?path=/src/a%20b%23c/%C3%A9.ts&version=GChead1",
      "https://dev.azure.com/acme/web/_git/app?path=/README.md&version=GChead1",
    ],
  ] as const)(
    "links a %s file from a fork, and a same-repo file, at the head",
    (provider, forkLink, sameRepoLink) => {
      const input = {
        headSha: "head1",
        baseSha: "base1",
        baseRepoUrl: BASE_REPO[provider],
        provider,
        status: "modified" as const,
      };
      expect(
        pullRequestFileUrl({
          ...input,
          headRepoUrl: FORK_REPO[provider],
          path: "src/a b#c/é.ts",
        }),
      ).toBe(forkLink);
      expect(
        pullRequestFileUrl({
          ...input,
          headRepoUrl: BASE_REPO[provider],
          path: "README.md",
          status: "created",
        }),
      ).toBe(sameRepoLink);
    },
  );

  it.each([
    ["github", "https://github.com/fork/app/blob/head1/src/new%20name.ts"],
    ["gitlab", "https://gitlab.com/fork/app/-/blob/head1/src/new%20name.ts"],
    [
      "azure_devops",
      "https://dev.azure.com/fork/web/_git/app?path=/src/new%20name.ts&version=GChead1",
    ],
  ] as const)("links a renamed %s file by its new path", (provider, link) => {
    expect(
      pullRequestFileUrl({
        provider,
        headRepoUrl: FORK_REPO[provider],
        baseRepoUrl: BASE_REPO[provider],
        headSha: "head1",
        baseSha: "base1",
        path: "src/new name.ts",
        previousPath: "old/name.ts",
        status: "renamed",
      }),
    ).toBe(link);
  });

  it.each([
    ["github", "https://github.com/acme/app/blob/base1/old/gone.ts"],
    ["gitlab", "https://gitlab.com/acme/app/-/blob/base1/old/gone.ts"],
    ["azure_devops", "https://dev.azure.com/acme/web/_git/app?path=/old/gone.ts&version=GCbase1"],
  ] as const)(
    "links a deleted %s file to the base repository at the base revision",
    (provider, link) => {
      expect(
        pullRequestFileUrl({
          provider,
          headRepoUrl: FORK_REPO[provider],
          baseRepoUrl: BASE_REPO[provider],
          headSha: "head1",
          baseSha: "base1",
          path: "old/gone.ts",
          status: "deleted",
        }),
      ).toBe(link);
    },
  );

  it("links a deleted file to the base revision when the head repository is absent", () => {
    expect(
      pullRequestFileUrl({
        provider: "github",
        baseRepoUrl: BASE_REPO.github,
        headSha: "head1",
        baseSha: "base1",
        path: "old/gone.ts",
        status: "deleted",
      }),
    ).toBe("https://github.com/acme/app/blob/base1/old/gone.ts");
  });

  it("uses the old path of a deleted file when the list carries one", () => {
    expect(
      pullRequestFileUrl({
        provider: "github",
        headRepoUrl: null,
        baseRepoUrl: BASE_REPO.github,
        headSha: "head1",
        baseSha: "base1",
        path: "new.ts",
        previousPath: "old.ts",
        status: "deleted",
      }),
    ).toBe("https://github.com/acme/app/blob/base1/old.ts");
  });

  it("links a deleted file in a repository named like the PR route marker", () => {
    expect(
      pullRequestFileUrl({
        provider: "github",
        baseRepoUrl: pullRequestRepoUrl("github", "https://github.com/pull/app/pull/7"),
        headSha: "head1",
        baseSha: "base1",
        path: "old/gone.ts",
        status: "deleted",
      }),
    ).toBe("https://github.com/pull/app/blob/base1/old/gone.ts");
  });

  it("keeps a numeric repository path whole for a deleted file", () => {
    expect(
      pullRequestFileUrl({
        provider: "github",
        baseRepoUrl: pullRequestRepoUrl("github", "https://github.com/pull/123/pull/7"),
        headSha: "head1",
        baseSha: "base1",
        path: "old/gone.ts",
        status: "deleted",
      }),
    ).toBe("https://github.com/pull/123/blob/base1/old/gone.ts");
  });

  it.each([
    ["null head repository", { status: "modified" as const, headRepoUrl: null }],
    [
      "absent head repository (older host)",
      { status: "modified" as const, headRepoUrl: undefined },
    ],
    ["an empty head repository", { status: "modified" as const, headRepoUrl: "" }],
    ["an unknown head revision", { status: "modified" as const, headSha: null }],
    ["a missing head revision", { status: "modified" as const, headSha: undefined }],
    [
      "a non-https head repository",
      { status: "modified" as const, headRepoUrl: "http://github.com/fork/app" },
    ],
    ["an unsupported provider", { status: "modified" as const, provider: "bitbucket" }],
    ["no provider", { status: "modified" as const, provider: null }],
    ["a deleted file with no base repository", { status: "deleted" as const, baseRepoUrl: null }],
    ["a deleted file with no base revision", { status: "deleted" as const, baseSha: null }],
    [
      "a deleted file with a non-https base repository",
      { status: "deleted" as const, baseRepoUrl: "http://github.com/acme/app" },
    ],
  ])("returns null for %s", (_label, override) => {
    const link = pullRequestFileUrl({
      provider: "github",
      headRepoUrl: FORK_REPO.github,
      baseRepoUrl: BASE_REPO.github,
      headSha: "head1",
      baseSha: "base1",
      path: "README.md",
      ...override,
    });
    expect(link).toBeNull();
  });
});
