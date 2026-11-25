// One record per PR and reader: who owns a read, which keys and filters reach which owner, and
// when an answer may go into a record, with real scopes and a real QueryClient.

import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const identity = vi.hoisted(() => ({ userId: "user-1" as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.userId,
}));
const provenance = vi.hoisted(() => ({ proves: true }));
vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  macAnswersCarryProvenance: () => provenance.proves,
}));

import { ReaderMovedError, type MacRecordPublication } from "@/lib/desktopPullRequests";
import {
  acceptMacRead,
  advanceMacReadScope,
  beginMacRead,
  macNamedReadScopeKey,
  macReadEpoch,
  macReadScopeKey,
  refuseMacReadScope,
  resetMacReadScopesForTests,
  type MacReadTicket,
} from "@/lib/macReadScope";

import {
  invalidatePullRequestEverywhere,
  isPullRequestRecordKey,
  joinBootstrapRead,
  publicationFor,
  publishPullRequestRead,
  pullRequestReadRestAt,
  pullRequestRecordFilter,
  pullRequestRecordKey,
  pullRequestRecordOwner,
  pullRequestRecordRead,
  readMoreOfRecord,
  readRecord,
  recordGcTime,
  type PullRequestRecordOwner,
} from "./pullRequestRecord";

const PR = "https://github.com/acme/app/pull/7";
const OTHER_PR = "https://github.com/acme/app/pull/8";
const GITHUB = { provider: "github" as const };
const scope = () => macReadScopeKey("github", "github.com");

/** A read answered as `viewer` under `ticket`, accepted by the scope as main would. */
function answered(ticket: MacReadTicket, viewer: string, publication: MacRecordPublication | null) {
  const verdict = acceptMacRead(ticket, viewer);
  if (verdict !== "rejected" && publication) publication.published = { viewer, ticket };
  return verdict;
}

beforeEach(() => {
  identity.userId = "user-1";
  provenance.proves = true;
});
afterEach(() => resetMacReadScopesForTests());

describe("pullRequestRecordOwner", () => {
  it("shares a host-wide Mac read once its shell proves who read it", () => {
    expect(pullRequestRecordOwner("conv_1", scope(), GITHUB)).toEqual({
      kind: "mac",
      scope: scope(),
      reader: null,
      epoch: 0,
    });
    answered(beginMacRead(scope()), "alice", null);
    expect(pullRequestRecordOwner("conv_1", scope(), GITHUB)).toMatchObject({ reader: "alice" });
  });

  it("keeps a session's own key where the shell proves nothing, or the read goes to the host", () => {
    provenance.proves = false;
    expect(pullRequestRecordOwner("conv_1", scope(), GITHUB)).toEqual({
      kind: "session",
      session: "conv_1",
      scope: scope(),
    });
    provenance.proves = true;
    expect(pullRequestRecordOwner("conv_1", scope(), null).kind).toBe("session");
    expect(pullRequestRecordOwner("conv_1", ["host", "conv_1"], GITHUB).kind).toBe("session");
    identity.userId = null;
    expect(
      pullRequestRecordOwner("conv_1", macReadScopeKey("github", "github.com", null), GITHUB),
    ).toMatchObject({ kind: "session" });
  });

  it("names a named read's login as its reader, proven by name on any shell", () => {
    provenance.proves = false;
    const named = macNamedReadScopeKey("github.com", "Bob");
    expect(pullRequestRecordOwner("page", named, GITHUB)).toMatchObject({
      kind: "mac",
      reader: "bob",
    });
  });
});

describe("record keys", () => {
  it("are one per PR, family, reader and epoch, whichever session reads them", () => {
    answered(beginMacRead(scope()), "alice", null);
    const fromA = pullRequestRecordRead("github-pr-diff", "conv_a", PR, scope(), GITHUB, "b:h");
    const fromB = pullRequestRecordRead("github-pr-diff", "conv_b", PR, scope(), GITHUB, "b:h");
    const fromPage = pullRequestRecordRead("github-pr-diff", "page", PR, scope(), GITHUB, "b:h");
    expect(fromA.queryKey).toEqual(fromB.queryKey);
    expect(fromA.queryKey).toEqual(fromPage.queryKey);
    expect(fromA.queryKey).toEqual([
      "github-pr-diff",
      "pr",
      PR,
      ...scope(),
      "reader",
      "alice",
      0,
      "b:h",
    ]);
    expect(isPullRequestRecordKey(fromA.queryKey)).toBe(true);
    expect(pullRequestReadRestAt(fromA.queryKey)).toBe(fromA.queryKey.length - 1);
  });

  it("move to a new epoch on every lifecycle move, so the same login back reaches nothing kept", () => {
    const key = () => pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB).queryKey;
    answered(beginMacRead(scope()), "alice", null);
    const aliceFirst = key();
    answered(beginMacRead(scope()), "bob", null);
    const bob = key();
    answered(beginMacRead(scope()), "alice", null);
    const aliceAgain = key();
    expect(new Set([aliceFirst, bob, aliceAgain].map((k) => JSON.stringify(k))).size).toBe(3);

    refuseMacReadScope(scope());
    answered(beginMacRead(scope()), "alice", null);
    const afterRefusal = key();
    advanceMacReadScope(scope(), "left");
    answered(beginMacRead(scope()), "alice", null);
    const afterLeave = key();
    const all = [aliceFirst, bob, aliceAgain, afterRefusal, afterLeave].map((k) =>
      JSON.stringify(k),
    );
    expect(new Set(all).size).toBe(all.length);
  });

  it("don't move on a refusal with no reader, so a refused first read isn't read again on its own", () => {
    const before = macReadEpoch(scope());
    refuseMacReadScope(scope());
    expect(macReadEpoch(scope())).toBe(before);
    answered(beginMacRead(scope()), "alice", null);
    refuseMacReadScope(scope());
    expect(macReadEpoch(scope())).toBe(before + 1);
    refuseMacReadScope(scope());
    expect(macReadEpoch(scope())).toBe(before + 1);
  });

  it("keep a session's layout for a session's own read", () => {
    provenance.proves = false;
    const owner = pullRequestRecordOwner("conv_1", scope(), GITHUB);
    expect(pullRequestRecordKey("github-changed-files", PR, owner, "b:h")).toEqual([
      "github-changed-files",
      "conv_1",
      PR,
      "b:h",
      ...scope(),
    ]);
  });
});

describe("pullRequestRecordFilter", () => {
  it("reaches only its own owner's entries of its family, every revision of them", () => {
    const client = new QueryClient();
    answered(beginMacRead(scope()), "alice", null);
    const alice = pullRequestRecordOwner("conv_a", scope(), GITHUB);
    const named = pullRequestRecordOwner("page", macNamedReadScopeKey("github.com", "bob"), GITHUB);
    provenance.proves = false;
    const hostA = pullRequestRecordOwner("conv_a", ["host", "a"], null);
    const hostB = pullRequestRecordOwner("conv_b", ["host", "b"], null);
    const owners: Record<string, PullRequestRecordOwner> = { alice, named, hostA, hostB };
    for (const [name, owner] of Object.entries(owners)) {
      for (const family of ["github-pr-diff", "github-changed-files"] as const) {
        for (const revision of ["b:h1", "b:h2"]) {
          client.setQueryData(pullRequestRecordKey(family, PR, owner, revision), { name });
        }
      }
      client.setQueryData(pullRequestRecordKey("github-pr-diff", OTHER_PR, owner, "b:h1"), {
        name,
      });
    }
    for (const [name, owner] of Object.entries(owners)) {
      const reached = client
        .getQueryCache()
        .findAll(pullRequestRecordFilter("github-pr-diff", PR, owner))
        .map((query) => query.queryKey);
      expect(reached).toHaveLength(2);
      for (const key of reached) {
        expect(key[0]).toBe("github-pr-diff");
        expect(key[2]).toBe(PR);
        expect(client.getQueryData(key)).toEqual({ name });
      }
    }
  });

  it("refreshes every owner's entries of a PR, and nothing of another PR", async () => {
    const client = new QueryClient();
    answered(beginMacRead(scope()), "alice", null);
    const alice = pullRequestRecordOwner("conv_a", scope(), GITHUB);
    provenance.proves = false;
    const host = pullRequestRecordOwner("conv_b", ["host", "b"], null);
    const keys = [
      pullRequestRecordKey("github-info", PR, alice),
      pullRequestRecordKey("github-review-threads", PR, host, "", "b:h"),
    ];
    const other = pullRequestRecordKey("github-info", OTHER_PR, alice);
    for (const key of [...keys, other]) client.setQueryData(key, {});
    await invalidatePullRequestEverywhere(client, PR);
    for (const key of keys) expect(client.getQueryState(key)?.isInvalidated).toBe(true);
    expect(client.getQueryState(other)?.isInvalidated).toBe(false);
  });
});

describe("publishPullRequestRead", () => {
  it("refuses an answer read as another account than the record's, read in its own query", async () => {
    // Alice's record reads again; the scope's account moved to Bob while it ran.
    const client = new QueryClient();
    answered(beginMacRead(scope()), "alice", null);
    const record = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    const reading = readRecord(client, record, { provider: "github" }, async (target) => {
      answered(beginMacRead(scope()), "bob", target!.publication!);
      return { body: "bob's" };
    });
    await expect(reading).rejects.toBeInstanceOf(ReaderMovedError);
    expect(client.getQueryData(record.queryKey)).toBeUndefined();
  });

  it("refuses a record's answer that no Mac read checked", () => {
    const client = new QueryClient();
    answered(beginMacRead(scope()), "alice", null);
    const { owner, queryKey } = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    expect(() =>
      publishPullRequestRead(client, queryKey, owner, publicationFor(owner), {}, Date.now()),
    ).toThrow(ReaderMovedError);
  });

  it("moves a bootstrap answer into its proven reader's record, keeping when it was read", async () => {
    const client = new QueryClient();
    const record = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    expect(record.owner).toMatchObject({ reader: null });
    const readAt = Date.now() - 5_000;
    await readRecord(
      client,
      record,
      { provider: "github" },
      async (target) => {
        answered(beginMacRead(scope()), "alice", target!.publication!);
        return { title: "Fix" };
      },
      readAt,
    );
    const aliceKey = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB).queryKey;
    expect(aliceKey).toContain("alice");
    expect(client.getQueryData(aliceKey)).toEqual({ title: "Fix" });
    expect(client.getQueryState(aliceKey)?.dataUpdatedAt).toBe(readAt);
    expect(recordGcTime(record.owner, 60_000)).toBe(0);
  });

  it("never lets an older bootstrap answer replace a newer one, whichever lands last", async () => {
    const client = new QueryClient();
    const bootstrap = pullRequestRecordRead("github-pr-diff", "conv_1", PR, scope(), GITHUB, "b:h");
    // Two bootstrap reads go out; the newer one is accepted first and proves Alice.
    const older = beginMacRead(scope());
    const newer = beginMacRead(scope());
    const olderPublication = publicationFor(bootstrap.owner)!;
    const newerPublication = publicationFor(bootstrap.owner)!;
    answered(newer, "alice", newerPublication);
    publishPullRequestRead(client, bootstrap.queryKey, bootstrap.owner, newerPublication, "new", 2);
    answered(older, "alice", olderPublication);
    publishPullRequestRead(client, bootstrap.queryKey, bootstrap.owner, olderPublication, "old", 1);
    const aliceKey = pullRequestRecordRead(
      "github-pr-diff",
      "c",
      PR,
      scope(),
      GITHUB,
      "b:h",
    ).queryKey;
    expect(client.getQueryData(aliceKey)).toBe("new");
  });

  it("promotes each family's bootstrap answer on its own, in any order", async () => {
    const client = new QueryClient();
    const families = ["github-info", "github-changed-files", "github-pr-diff"] as const;
    const reads = families.map((family) => ({
      record: pullRequestRecordRead(family, "conv_1", PR, scope(), GITHUB),
      ticket: beginMacRead(scope()),
      publication: publicationFor(pullRequestRecordOwner("conv_1", scope(), GITHUB))!,
    }));
    for (const { record, ticket, publication } of [...reads].reverse()) {
      answered(ticket, "alice", publication);
      publishPullRequestRead(
        client,
        record.queryKey,
        record.owner,
        publication,
        record.queryKey[0],
        1,
      );
    }
    for (const family of families) {
      const key = pullRequestRecordRead(family, "conv_1", PR, scope(), GITHUB).queryKey;
      expect(client.getQueryData(key)).toBe(family);
    }
  });

  it("writes nothing for a bootstrap answer that lands after its scope moved", () => {
    const client = new QueryClient();
    const bootstrap = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    const ticket = beginMacRead(scope());
    const publication = publicationFor(bootstrap.owner)!;
    answered(ticket, "alice", publication);
    advanceMacReadScope(scope());
    expect(() =>
      publishPullRequestRead(client, bootstrap.queryKey, bootstrap.owner, publication, "late", 1),
    ).toThrow(ReaderMovedError);
    expect(client.getQueryCache().findAll({ queryKey: ["github-info"] })).toHaveLength(0);
  });

  it("refuses an answer once the workspace user changed", () => {
    const client = new QueryClient();
    answered(beginMacRead(scope()), "alice", null);
    const record = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    const publication = publicationFor(record.owner)!;
    answered(beginMacRead(scope()), "alice", publication);
    identity.userId = "user-2";
    expect(() =>
      publishPullRequestRead(client, record.queryKey, record.owner, publication, {}, 1),
    ).toThrow(ReaderMovedError);
  });

  it("leaves a session's own read as it was, proven or not", () => {
    provenance.proves = false;
    const client = new QueryClient();
    const record = pullRequestRecordRead("github-info", "conv_1", PR, scope(), GITHUB);
    expect(() =>
      publishPullRequestRead(client, record.queryKey, record.owner, null, {}, 1),
    ).not.toThrow();
  });
});

describe("joinBootstrapRead", () => {
  it("gives a reader's record the bootstrap answer still out, rather than reading again", async () => {
    const client = new QueryClient();
    const bootstrap = pullRequestRecordRead("github-pr-diff", "conv_1", PR, scope(), GITHUB, "b:h");
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const reading = readRecord(client, bootstrap, { provider: "github" }, async (target) => {
      answered(beginMacRead(scope()), "alice", target!.publication!);
      await gate;
      return "patch";
    });
    // The view moves to Alice's record as soon as Alice is proven.
    const aliceRead = () =>
      pullRequestRecordRead("github-pr-diff", "conv_1", PR, scope(), GITHUB, "b:h");
    await vi.waitFor(() => expect(aliceRead().owner).toMatchObject({ reader: "alice" }));
    const alice = aliceRead();
    client.getQueryCache().build(client, { queryKey: alice.queryKey });
    const joined = joinBootstrapRead<string>(client, alice);
    release();
    await reading;
    await expect(joined).resolves.toBe("patch");
    // With nothing out, the record reads for itself.
    await expect(joinBootstrapRead(client, alice)).resolves.toBeUndefined();
  });
});

describe("readMoreOfRecord", () => {
  it("refuses a next page read as another account than its record's", async () => {
    answered(beginMacRead(scope()), "alice", null);
    const record = pullRequestRecordRead(
      "github-review-threads",
      "c",
      PR,
      scope(),
      GITHUB,
      "",
      "r",
    );
    await expect(
      readMoreOfRecord(record, { provider: "github" }, async (target) => {
        answered(beginMacRead(scope()), "bob", target!.publication!);
        return "page 2";
      }),
    ).rejects.toBeInstanceOf(ReaderMovedError);
    const fresh = pullRequestRecordRead("github-review-threads", "c", PR, scope(), GITHUB, "", "r");
    await expect(
      readMoreOfRecord(fresh, { provider: "github" }, async (target) => {
        answered(beginMacRead(scope()), "bob", target!.publication!);
        return "page 2";
      }),
    ).resolves.toBe("page 2");
  });
});
