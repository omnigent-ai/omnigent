import { describe, expect, it } from "vitest";

import {
  listRemovalOf,
  ownsSessionTree,
  removalDescription,
  removalOutcome,
  removalPlace,
  rowRemoval,
  rowRemovals,
  unlinkPlan,
} from "./subAgentRemoval";

const PR = "https://github.com/acme/app/pull/7";
const VIEWED = "conv_parent";
const AUTH = { id: "conv_auth", name: "auth-fix" };
const DOCS = { id: "conv_docs", name: "docs" };
const TESTS = { id: "conv_tests", name: "tests" };

describe("ownsSessionTree", () => {
  it("is the root's owner grant naming the viewer", () => {
    expect(ownsSessionTree("ada@example.com", "ada@example.com")).toBe(true);
  });

  it("isn't for a tree shared with edit access, or seen by an admin who doesn't own it", () => {
    expect(ownsSessionTree("ada@example.com", "bob@example.com")).toBe(false);
  });

  it("is only the single user's where the server names no owner", () => {
    expect(ownsSessionTree(null, "local")).toBe(true);
    expect(ownsSessionTree(null, "bob@example.com")).toBe(false);
  });

  it("isn't known while the owner or the viewer is still being read", () => {
    expect(ownsSessionTree(undefined, "ada@example.com")).toBeUndefined();
    expect(ownsSessionTree("ada@example.com", null)).toBeUndefined();
  });
});

describe("rowRemoval", () => {
  const removal = (
    source: Parameters<typeof rowRemoval>[0]["source"],
    options: { ownsTree?: boolean | undefined; canEditViewed?: boolean } = {},
  ) =>
    rowRemoval({
      url: PR,
      source,
      viewedSessionId: VIEWED,
      canEditViewed: options.canEditViewed ?? true,
      ownsTree: "ownsTree" in options ? options.ownsTree : true,
    });

  it("removes the viewed session's own link, as before", () => {
    expect(removal(undefined)).toEqual({
      url: PR,
      targets: [{ sessionId: VIEWED, url: PR }],
      fromSubAgents: [],
      kept: [],
      ownershipPending: false,
    });
  });

  it("removes a sub-agent's link, and every other sub-agent's that links the same PR", () => {
    expect(removal({ session: AUTH, alsoIn: [DOCS] }).targets).toEqual([
      { sessionId: AUTH.id, url: PR },
      { sessionId: DOCS.id, url: PR },
    ]);
  });

  it("removes the viewed session's link with its sub-agents' where both link it", () => {
    expect(removal({ session: null, alsoIn: [AUTH] }).targets.map((t) => t.sessionId)).toEqual([
      VIEWED,
      AUTH.id,
    ]);
  });

  it("leaves sub-agents' links alone, and names them, where the user doesn't own the tree", () => {
    for (const ownsTree of [false, undefined]) {
      const shared = removal({ session: null, alsoIn: [AUTH] }, { ownsTree });
      expect(shared.targets).toEqual([{ sessionId: VIEWED, url: PR }]);
      expect(shared.kept).toEqual([AUTH]);
    }
    expect(removal({ session: AUTH, alsoIn: [] }, { ownsTree: false }).targets).toEqual([]);
  });

  it("never removes the viewed session's link without edit access", () => {
    expect(removal(undefined, { canEditViewed: false }).targets).toEqual([]);
  });

  it("lists a session once though it shows up twice", () => {
    expect(removal({ session: AUTH, alsoIn: [AUTH, DOCS] }).targets).toHaveLength(2);
  });
});

describe("rowRemovals", () => {
  it("takes each shown PR once by its canonical URL, and nothing for a URL the list doesn't show", () => {
    const shown = rowRemoval({
      url: PR,
      source: { session: AUTH, alsoIn: [] },
      viewedSessionId: VIEWED,
      canEditViewed: true,
      ownsTree: true,
    });
    const removals = rowRemovals([PR, `${PR}/`, "https://github.com/acme/app/pull/99"], (url) =>
      url.startsWith(PR) ? shown : undefined,
    );
    expect(removals).toEqual([shown]);
  });
});

describe("removalDescription", () => {
  const describeRemoval = (
    source: Parameters<typeof rowRemoval>[0]["source"],
    ownsTree: boolean | undefined = true,
  ) =>
    removalDescription(
      rowRemoval({ url: PR, source, viewedSessionId: VIEWED, canEditViewed: true, ownsTree }),
    );

  it("names the one agent a sub-agent's row is removed from", () => {
    expect(describeRemoval({ session: AUTH, alsoIn: [] })).toEqual({
      menuLabel: "Remove from auth-fix",
      detail: null,
      unavailableReason: null,
    });
  });

  it("counts the agents a row linked several times is removed from, and names them", () => {
    expect(describeRemoval({ session: null, alsoIn: [AUTH, DOCS] })).toEqual({
      menuLabel: "Remove from 3 agents",
      detail: "Unlinks it from this session, auth-fix and docs.",
      unavailableReason: null,
    });
    expect(describeRemoval({ session: AUTH, alsoIn: [DOCS] }).detail).toBe(
      "Unlinks it from auth-fix and docs.",
    );
  });

  it("says which sources it leaves linked, and why a row with only those can't be removed", () => {
    expect(describeRemoval({ session: null, alsoIn: [AUTH] }, false)).toEqual({
      menuLabel: "Remove from this session",
      detail: "Still linked by auth-fix, which you don’t own.",
      unavailableReason: null,
    });
    expect(describeRemoval({ session: AUTH, alsoIn: [DOCS, TESTS] }, false)).toEqual({
      menuLabel: "Remove from this session",
      detail: null,
      unavailableReason: "Still linked by auth-fix, docs and tests, which you don’t own.",
    });
  });

  it("says it's still checking who owns the tree rather than that the user doesn't", () => {
    const unknown = (source: Parameters<typeof rowRemoval>[0]["source"]) =>
      removalDescription(
        rowRemoval({
          url: PR,
          source,
          viewedSessionId: VIEWED,
          canEditViewed: true,
          ownsTree: undefined,
        }),
      );
    expect(unknown({ session: AUTH, alsoIn: [] }).unavailableReason).toBe(
      "Checking whether you own auth-fix…",
    );
    expect(unknown({ session: null, alsoIn: [AUTH] }).detail).toBe(
      "Checking whether you own auth-fix…",
    );
  });
});

describe("listRemovalOf", () => {
  const removalOf = (shown: string[], ownsTree = true) =>
    listRemovalOf({
      shown,
      sources: new Map([[PR, { session: AUTH, alsoIn: [] }]]),
      viewedSessionId: VIEWED,
      canEditViewed: true,
      ownsTree,
    });

  it("removes a shown row by any spelling of its URL, with the row's own sources", () => {
    expect(removalOf([PR])(`${PR}/`)).toEqual({
      url: PR,
      targets: [{ sessionId: AUTH.id, url: PR }],
      fromSubAgents: [AUTH],
      kept: [],
      ownershipPending: false,
    });
  });

  it("removes nothing for a URL the list no longer shows, rather than taking it as its own", () => {
    expect(removalOf([])(PR)).toBeUndefined();
    expect(removalOf([PR])("https://github.com/acme/app/pull/99")).toBeUndefined();
  });
});

describe("unlinkPlan", () => {
  const OWN = "https://github.com/acme/app/pull/8";
  const STALE = "https://github.com/acme/app/pull/99";
  const removalOf = (ownsTree: boolean) =>
    listRemovalOf({
      shown: [PR, OWN],
      sources: new Map([[PR, { session: AUTH, alsoIn: [] }]]),
      viewedSessionId: VIEWED,
      canEditViewed: true,
      ownsTree,
    });

  it("does nothing for URLs the list no longer shows, never unlinking them as its own", () => {
    expect(unlinkPlan([STALE], removalOf(true))).toEqual({ kind: "none" });
  });

  it("unlinks only the shown own links, dropping stale ones and those it leaves alone", () => {
    expect(unlinkPlan([`${OWN}/`, STALE], removalOf(true))).toEqual({ kind: "own", urls: [OWN] });
    // A sub-agent's row in a tree the user doesn't own unlinks nothing of its own.
    expect(unlinkPlan([PR], removalOf(false))).toEqual({ kind: "none" });
  });

  it("runs across the tree where a row reaches a sub-agent's link", () => {
    const plan = unlinkPlan([PR, OWN, STALE], removalOf(true));
    expect(plan.kind).toBe("tree");
    expect(plan.kind === "tree" && plan.removals.map((removal) => removal.url)).toEqual([PR, OWN]);
  });
});

describe("removalPlace", () => {
  const removal = (source: Parameters<typeof rowRemoval>[0]["source"], ownsTree = true) =>
    rowRemoval({ url: PR, source, viewedSessionId: VIEWED, canEditViewed: true, ownsTree });

  it("keeps the label short and names every session in its detail", () => {
    expect(removalPlace([removal({ session: null, alsoIn: [AUTH, DOCS, TESTS] })])).toEqual({
      text: "this session and its agents",
      title: "This Session and Its Agents",
      detail: "Unlinks them from this session, auth-fix, docs and tests.",
    });
  });

  it("names every session it leaves linked", () => {
    expect(removalPlace([removal({ session: null, alsoIn: [AUTH, DOCS] }, false)])).toEqual({
      text: "this session",
      title: "This Session",
      detail: "Still linked by auth-fix and docs, which you don’t own.",
    });
  });
});

describe("removalOutcome", () => {
  const twoSources = rowRemoval({
    url: PR,
    source: { session: AUTH, alsoIn: [DOCS] },
    viewedSessionId: VIEWED,
    canEditViewed: true,
    ownsTree: true,
  });

  it("is removed only once every link went", () => {
    expect(removalOutcome(twoSources, new Set([AUTH.id, DOCS.id]))).toBe("removed");
  });

  it("is partial when one source refused or was never sent after Stop", () => {
    expect(removalOutcome(twoSources, new Set([AUTH.id]))).toBe("partial");
  });

  it("is not removed when no link went", () => {
    expect(removalOutcome(twoSources, new Set())).toBe("not_removed");
  });

  it("is partial when a source the user doesn't own still links it", () => {
    const kept = rowRemoval({
      url: PR,
      source: { session: null, alsoIn: [AUTH] },
      viewedSessionId: VIEWED,
      canEditViewed: true,
      ownsTree: false,
    });
    expect(removalOutcome(kept, new Set([VIEWED]))).toBe("partial");
  });
});
