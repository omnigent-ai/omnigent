import { describe, expect, it } from "vitest";

import {
  resolveStackOutline,
  stackPullRequestRef,
  type StackOutline,
  type StackOutlineEntry,
  type StackSelf,
} from "@/lib/pullRequestStacks/stackModel";

const SELF: StackSelf = { host: "git.example.com", repository: "acme/app", number: 102 };
const pullUrl = (number: number, repository = "acme/app", host = "git.example.com") =>
  `https://${host}/${repository}/pull/${number}`;

function entry(
  indent: number,
  branch: string,
  url: string | null,
  current = false,
): StackOutlineEntry {
  return { indent, branch, url, markers: { current, merged: false } };
}

function outline(members: StackOutlineEntry[], extra: Partial<StackOutline> = {}): StackOutline {
  return {
    format: "test",
    dependency: null,
    members,
    span: { start: 0, end: 10 },
    tolerances: [],
    ...extra,
  };
}

describe("stackPullRequestRef", () => {
  it("reads a pull request link's full identity", () => {
    expect(stackPullRequestRef("https://Git.Example.com/acme/app/pull/7")).toEqual({
      url: "https://Git.Example.com/acme/app/pull/7",
      host: "git.example.com",
      repository: "acme/app",
      number: 7,
    });
  });

  it.each([
    "http://git.example.com/acme/app/pull/7",
    "https://git.example.com/acme/app/pull/7/files",
    "https://git.example.com/acme/app/issues/7",
    "https://git.example.com/acme/app/pull/0",
    "https://git.example.com/acme/app/pull/7?x=1",
    "https://git.example.com:8443/acme/app/pull/7",
    "https://git.example.com:443/acme/app/pull/7",
    "https://user@git.example.com/acme/app/pull/7",
    "https://user:secret@git.example.com/acme/app/pull/7",
    "https://git.example.com/acme/./app/pull/7",
    "not a url",
  ])("rejects %s", (url) => {
    expect(stackPullRequestRef(url)).toBeNull();
  });
});

describe("resolveStackOutline", () => {
  it("parents a PR stacked on an unpushed branch to that branch, not the PR below it", () => {
    const parsed = resolveStackOutline(
      outline([
        entry(0, "stack/base", pullUrl(100)),
        entry(2, "stack/unpushed", null),
        entry(4, "stack/top", pullUrl(102), true),
      ]),
      SELF,
    );
    expect(parsed?.nodes.map((node) => [node.branch, node.depth, node.parentIndex])).toEqual([
      ["stack/base", 0, null],
      ["stack/unpushed", 1, 0],
      ["stack/top", 2, 1],
    ]);
    expect(parsed?.selfIndex).toBe(2);
    expect(parsed?.trust).toBe("verified");
  });

  it("never binds this PR through a link to another port or with credentials", () => {
    for (const url of [
      "https://git.example.com:8443/acme/app/pull/102",
      "https://user:secret@git.example.com/acme/app/pull/102",
    ]) {
      expect(resolveStackOutline(outline([entry(0, "only", url, true)]), SELF)).toBeNull();
      const withParent = resolveStackOutline(
        outline([entry(0, "base", url.replace("102", "100")), entry(2, "top", pullUrl(102), true)]),
        SELF,
      );
      expect(withParent).toMatchObject({ trust: "tolerated", tolerances: ["link"], selfIndex: 1 });
      expect(withParent?.nodes[0]?.ref).toBeNull();
    }
  });

  it("accepts a trailing slash on a PR link", () => {
    expect(resolveStackOutline(outline([entry(0, "a", `${pullUrl(102)}/`)]), SELF)?.trust).toBe(
      "verified",
    );
  });

  it("gives siblings the same parent", () => {
    const parsed = resolveStackOutline(
      outline([
        entry(0, "a", pullUrl(100)),
        entry(2, "b", pullUrl(101)),
        entry(2, "c", pullUrl(102), true),
      ]),
      SELF,
    );
    expect(parsed?.nodes.map((node) => node.parentIndex)).toEqual([null, 0, 0]);
  });

  it("parents an entry that skips a level to the nearest shallower entry", () => {
    const parsed = resolveStackOutline(
      outline([entry(0, "a", pullUrl(100)), entry(6, "b", pullUrl(102), true)]),
      SELF,
    );
    expect(parsed?.nodes.map((node) => [node.depth, node.parentIndex])).toEqual([
      [0, null],
      [1, 0],
    ]);
  });

  it("binds this PR by its full identity and rejects a description that doesn't list it", () => {
    expect(resolveStackOutline(outline([entry(0, "a", pullUrl(100), true)]), SELF)).toBeNull();
    // Same number in another repository or on another host isn't this PR.
    expect(
      resolveStackOutline(outline([entry(0, "a", pullUrl(102, "acme/other"))]), SELF),
    ).toBeNull();
    expect(
      resolveStackOutline(
        outline([entry(0, "a", pullUrl(102, "acme/app", "other.example.com"))]),
        SELF,
      ),
    ).toBeNull();
    expect(
      resolveStackOutline(outline([entry(0, "a", pullUrl(102, "ACME/App"))]), SELF)?.selfIndex,
    ).toBe(0);
  });

  it("marks entries in other repositories and on other hosts as external", () => {
    const parsed = resolveStackOutline(
      outline([
        entry(0, "a", pullUrl(5, "other/lib")),
        entry(2, "b", pullUrl(6, "acme/app", "other.example.com")),
        entry(4, "c", pullUrl(102), true),
      ]),
      SELF,
    );
    expect(parsed?.nodes.map((node) => node.external)).toEqual([true, true, false]);
  });

  it("tolerates a current marker on another PR, keeping this PR as self", () => {
    const parsed = resolveStackOutline(
      outline([entry(0, "a", pullUrl(100), true), entry(2, "b", pullUrl(102))]),
      SELF,
    );
    expect(parsed).toMatchObject({
      trust: "tolerated",
      tolerances: ["current-marker"],
      selfIndex: 1,
    });
  });

  it("tolerates duplicate entries, and the first copy of this PR is self", () => {
    const parsed = resolveStackOutline(
      outline([entry(0, "a", pullUrl(102), true), entry(2, "b", pullUrl(102))]),
      SELF,
    );
    expect(parsed).toMatchObject({ trust: "tolerated", selfIndex: 0 });
    expect(parsed?.trust === "tolerated" && parsed.tolerances).toContain("duplicate");
  });

  it("tolerates a link that names no pull request, keeping the entry as unpushed", () => {
    const parsed = resolveStackOutline(
      outline([
        entry(0, "a", "https://git.example.com/acme/app"),
        entry(2, "b", pullUrl(102), true),
      ]),
      SELF,
    );
    expect(parsed?.nodes[0]?.ref).toBeNull();
    expect(parsed).toMatchObject({ trust: "tolerated", tolerances: ["link"] });
  });

  it("keeps the dependency apart from the members and never treats it as this PR", () => {
    const parsed = resolveStackOutline(
      outline([entry(2, "b", pullUrl(102), true)], { dependency: entry(0, "dep", pullUrl(99)) }),
      SELF,
    );
    expect(parsed?.dependency?.ref?.number).toBe(99);
    expect(parsed?.nodes.map((node) => [node.depth, node.parentIndex])).toEqual([[0, null]]);
    expect(
      resolveStackOutline(
        outline([entry(2, "b", pullUrl(101))], { dependency: entry(0, "dep", pullUrl(102)) }),
        SELF,
      ),
    ).toBeNull();
  });

  it("is only verified with exact bounds and no tolerances", () => {
    expect(
      resolveStackOutline(outline([entry(0, "a", pullUrl(102))], { span: null }), SELF),
    ).toMatchObject({
      trust: "tolerated",
      tolerances: ["footer"],
    });
    expect(
      resolveStackOutline(outline([entry(0, "a", pullUrl(102))], { tolerances: ["header"] }), SELF),
    ).toMatchObject({
      trust: "tolerated",
      tolerances: ["header"],
    });
  });
});
