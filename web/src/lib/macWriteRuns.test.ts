// A bulk run's grants, page side: one asked for per account the run writes as, only from a
// shell with every part of the run bridge, and found again only for exactly the change it names,
// as the same workspace user, until the run ends.

import { afterEach, describe, expect, it, vi } from "vitest";

import { macWriteRunGrant, resetMacWriteRunsForTests, startMacWriteRun } from "./macWriteRuns";

interface Route {
  target: { host: string; repository: string; number: number };
  actor: { login: string; generation: number };
  reader: { host: string; login: string } | null;
}
const HOST_ROUTE: Route = {
  target: { host: "github.com", repository: "acme/app", number: 1 },
  actor: { login: "mac-octo", generation: 3 },
  reader: null,
};
const route = (number: number, over: Partial<Route> = {}) => ({
  ...HOST_ROUTE,
  ...over,
  target: { ...HOST_ROUTE.target, number, ...over.target },
});
const READY = "pull_request_ready_for_review";
const entry = (number: number, over: Partial<Route> = {}) => ({
  route: route(number, over),
  operation: READY,
  params: { repository: "acme/app", number },
});
const WRITES: { version: number; operations: string[]; runGrants?: number } = {
  version: 1,
  operations: [READY],
  runGrants: 1,
};

interface Bridge {
  authorize?: (request: { requestId: string; macLogin?: string }) => Promise<unknown>;
  authorizeAs?: (request: { requestId: string; macLogin?: string }) => Promise<unknown>;
  end?: (requestId: string) => void;
}

function install({ authorize, authorizeAs, end }: Bridge) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    ...(authorize ? { codeHostWriteAuthorizeRun: vi.fn(authorize) } : {}),
    ...(authorizeAs ? { codeHostWriteAuthorizeRunAs: vi.fn(authorizeAs) } : {}),
    ...(end ? { codeHostWriteEndRun: vi.fn(end) } : {}),
  };
  return (window as unknown as { omnigentDesktop: Record<string, ReturnType<typeof vi.fn>> })
    .omnigentDesktop;
}

const granted = (grantId: string) => async () => ({ ok: true, grantId });
const lookup = (number: number, over: Partial<Route> = {}, operation = READY) => ({
  owner: "user-1",
  authGeneration: 7,
  route: route(number, over),
  operation,
});

afterEach(() => {
  resetMacWriteRunsForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("macWriteRunGrant", () => {
  it("finds a grant only within its own run's send", async () => {
    install({ authorize: granted("grant-a"), end: () => {} });
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1)],
    });
    await run.ready;
    // An ordinary write, outside any run, never inherits one.
    expect(macWriteRunGrant(lookup(1))).toBeNull();
    expect(run.within(() => macWriteRunGrant(lookup(1)))).toBe("grant-a");
    expect(macWriteRunGrant(lookup(1))).toBeNull();
    // Ended: not even its own send carries it.
    run.end();
    expect(run.within(() => macWriteRunGrant(lookup(1)))).toBeNull();
  });

  it("keeps two runs on one pull request apart, each with its own grant", async () => {
    let next = 0;
    install({
      authorize: async () => ({ ok: true, grantId: `grant-${(next += 1)}` }),
      end: () => {},
    });
    const alice = { actor: { login: "alice", generation: 1 } };
    const bob = { actor: { login: "bob", generation: 1 } };
    const aliceRun = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1, alice)],
    });
    const bobRun = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1, bob)],
    });
    await Promise.all([aliceRun.ready, bobRun.ready]);
    expect(bobRun.within(() => macWriteRunGrant(lookup(1, bob)))).toBe("grant-2");
    expect(bobRun.within(() => macWriteRunGrant(lookup(1, alice)))).toBeNull();
    expect(aliceRun.within(() => macWriteRunGrant(lookup(1, alice)))).toBe("grant-1");
    // A send nested in another run's call is that inner run's own.
    expect(aliceRun.within(() => bobRun.within(() => macWriteRunGrant(lookup(1, bob))))).toBe(
      "grant-2",
    );
  });
});

describe("startMacWriteRun", () => {
  it("asks for one grant per account, naming each change, and finds it for exactly those", async () => {
    const api = install({ authorize: granted("grant-a"), end: () => {} });
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1), entry(2)],
    });
    // Asked in the click's own task, before anything is awaited.
    expect(api.codeHostWriteAuthorizeRun).toHaveBeenCalledTimes(1);
    expect(api.codeHostWriteAuthorizeRun.mock.calls[0]![0]).toMatchObject({
      provider: "github",
      host: "github.com",
      actor: { login: "mac-octo", generation: 3 },
      entries: [
        { operation: READY, params: { repository: "acme/app", number: 1 } },
        { operation: READY, params: { repository: "acme/app", number: 2 } },
      ],
    });
    expect(run.grantFor(lookup(1))).toBeNull();
    await run.ready;
    expect(run.grantFor(lookup(1))).toBe("grant-a");
    expect(run.grantFor(lookup(2))).toBe("grant-a");
    // Never for another PR, change, account, sign-in or workspace user.
    expect(run.grantFor(lookup(3))).toBeNull();
    expect(run.grantFor(lookup(1, {}, "pull_request_close"))).toBeNull();
    expect(run.grantFor(lookup(1, { actor: { login: "mac-octo", generation: 4 } }))).toBeNull();
    expect(run.grantFor({ ...lookup(1), owner: "user-2" })).toBeNull();
    expect(run.grantFor({ ...lookup(1), authGeneration: 8 })).toBeNull();
  });

  it("asks a login the page reads as through its own method, apart from the host's account", async () => {
    const api = install({
      authorize: granted("grant-host"),
      authorizeAs: granted("grant-named"),
      end: () => {},
    });
    const named = { reader: { host: "github.com", login: "mac-octo" } };
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1), entry(2, named)],
    });
    await run.ready;
    expect(api.codeHostWriteAuthorizeRunAs.mock.calls[0]![0]).toMatchObject({
      macLogin: "mac-octo",
    });
    expect(run.grantFor(lookup(1))).toBe("grant-host");
    expect(run.grantFor(lookup(2, named))).toBe("grant-named");
    expect(run.grantFor(lookup(1, named))).toBeNull();
  });

  it("ends every grant once, so none is found again, even one that answers afterwards", async () => {
    let answer!: (value: unknown) => void;
    const api = install({
      authorize: () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
      end: () => {},
    });
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1)],
    });
    const requestId = api.codeHostWriteAuthorizeRun.mock.calls[0]![0].requestId;
    run.end();
    run.end();
    answer({ ok: true, grantId: "late" });
    await run.ready;
    expect(api.codeHostWriteEndRun.mock.calls).toEqual([[requestId]]);
    expect(run.grantFor(lookup(1))).toBeNull();
  });

  it("finds nothing when the shell refused, failed or answered something else", async () => {
    for (const authorize of [
      async () => ({ ok: false, error: { code: "writes_disabled" } }),
      async () => {
        throw new Error("gone");
      },
      async () => ({ ok: true }),
      async () => null,
    ]) {
      install({ authorize, end: () => {} });
      const run = startMacWriteRun({
        owner: "user-1",
        authGeneration: 7,
        writes: WRITES,
        entries: [entry(1)],
      });
      // oxlint-disable-next-line no-await-in-loop -- each shell answers on its own
      await run.ready;
      expect(run.grantFor(lookup(1))).toBeNull();
      run.end();
    }
  });

  for (const [label, bridge, writes] of [
    [
      "a shell that doesn't offer run grants",
      { authorize: granted("g"), end: () => {} },
      { version: 1, operations: [READY] } as typeof WRITES,
    ],
    ["a shell that can't end a run", { authorize: granted("g") }, WRITES],
    ["a shell that can't authorize one", { end: () => {} }, WRITES],
  ] as const) {
    it(`asks ${label} for nothing`, async () => {
      const api = install(bridge);
      const run = startMacWriteRun({
        owner: "user-1",
        authGeneration: 7,
        writes,
        entries: [entry(1)],
      });
      await run.ready;
      expect(api.codeHostWriteAuthorizeRun?.mock.calls ?? []).toEqual([]);
      expect(run.capable).toBe(false);
      expect(run.grantFor(lookup(1))).toBeNull();
    });
  }

  it("asks nothing for a named login on a shell with only the host account's method", async () => {
    const api = install({ authorize: granted("g"), end: () => {} });
    const named = { reader: { host: "github.com", login: "mac-octo" } };
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [entry(1, named)],
    });
    await run.ready;
    expect(api.codeHostWriteAuthorizeRun.mock.calls).toEqual([]);
    expect(run.grantFor(lookup(1, named))).toBeNull();
  });

  it("names one batch entry for a whole batch's change", async () => {
    install({ authorize: granted("grant-batch"), end: () => {} });
    const batch = "pull_request_auto_merge_enable_many";
    const run = startMacWriteRun({
      owner: "user-1",
      authGeneration: 7,
      writes: WRITES,
      entries: [{ route: route(1), operation: batch, params: { members: [], listed: [] } }],
    });
    await run.ready;
    // The batch is sent as its first PR, whichever that is after its reads.
    expect(run.grantFor(lookup(2, {}, batch))).toBe("grant-batch");
  });
});
