import { notifyManager, QueryClient, QueryObserver } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  acceptMacAnswer,
  desktopReadTarget,
  macAccountChanged,
  macReadTicket,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  acceptMacRead,
  beginMacRead,
  macReadEpoch,
  macReadScopeKey,
  refuseMacReadScope,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";
import type * as IdentityModule from "@/lib/identity";
import { bindMacReadScopes, keyHasScope } from "./macReadScopeQueries";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

const SPOKE = "2.spoke.example.com";
const spokeScope = ["mac-read", "user-1", "gitspoke", SPOKE];

afterEach(() => {
  resetDesktopReadPathForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("keyHasScope", () => {
  it("finds the scope as consecutive parts anywhere in a key", () => {
    expect(keyHasScope(["github-info", "conv", "url", ...spokeScope], spokeScope)).toBe(true);
    expect(keyHasScope([...spokeScope, "suggestions"], spokeScope)).toBe(true);
    expect(keyHasScope(["mac-read", "user-1", "gitspoke", "3.example"], spokeScope)).toBe(false);
    expect(keyHasScope(["mac-read", "user-2", "gitspoke", SPOKE], spokeScope)).toBe(false);
    expect(keyHasScope(["anything"], [])).toBe(false);
  });
});

describe("bindMacReadScopes", () => {
  it("drops a scope's unused queries and reads its shown ones again, leaving others", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const unbind = bindMacReadScopes(client);
    const shownFn = vi.fn(async () => "as bob");
    client.setQueryData(["github-info", "conv", ...spokeScope], "as alice");
    client.setQueryData(["unused", ...spokeScope], "as alice");
    client.setQueryData(["github-info", "conv"], "host");
    const observer = new QueryObserver(client, {
      queryKey: ["github-info", "conv", ...spokeScope],
      queryFn: shownFn,
      staleTime: Infinity,
    });
    const unsubscribe = observer.subscribe(() => {});

    macAccountChanged("gitspoke", SPOKE);

    expect(client.getQueryCache().find({ queryKey: ["unused", ...spokeScope] })).toBeUndefined();
    expect(client.getQueryData(["github-info", "conv"])).toBe("host");
    await vi.waitFor(() =>
      expect(client.getQueryData(["github-info", "conv", ...spokeScope])).toBe("as bob"),
    );
    expect(shownFn).toHaveBeenCalledTimes(1);
    unsubscribe();
    unbind();
  });
});

describe("a scope that advances", () => {
  it("drops a read still in flight, so its late answer never shows", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    bindMacReadScopes(client);
    let answerFirst: (value: string) => void = () => {};
    const queryFn = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise<string>((resolve) => {
            answerFirst = resolve;
          }),
      )
      .mockResolvedValue("as bob");
    const seen: unknown[] = [];
    const observer = new QueryObserver(client, { queryKey: ["info", ...spokeScope], queryFn });
    const unsubscribe = observer.subscribe((result) => seen.push(result.data));
    await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(1));

    macAccountChanged("gitspoke", SPOKE);
    answerFirst("as alice");
    await vi.waitFor(() => expect(client.getQueryData(["info", ...spokeScope])).toBe("as bob"));
    await new Promise((resolve) => {
      setTimeout(resolve, 10);
    });
    expect(seen).not.toContain("as alice");
    expect(client.getQueryData(["info", ...spokeScope])).toBe("as bob");
    unsubscribe();
  });
});

/** How a held read settles. */
interface Settle {
  resolve: (value: string) => void;
  reject: (error: Error) => void;
}

describe("a change of the scope's reader, seen by either kind of read", () => {
  const SPOKE_PR = `https://${SPOKE}/example-org/universe/pull/7`;
  const panelAnswer = (viewer: string) => ({
    provenance: { version: 1 as const, provider: "gitspoke" as const, host: SPOKE, viewer },
  });

  /** A shell whose GitSpoke answers carry provenance, and a client bound to the scopes. */
  async function setup() {
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      codeHostQuery: async () => ({ ok: false, error: { code: "unsupported", message: "" } }),
      getDesktopFeatures: async () => ({
        codeHosts: [{ id: "gitspoke", operations: ["pull_request_overview"], answerProvenance: 1 }],
      }),
    };
    await desktopReadTarget(SPOKE_PR);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const unbind = bindMacReadScopes(client);
    return { client, unbind };
  }

  /** A shown query that answers `first`, then holds its next read. */
  function shown(client: QueryClient, queryKey: readonly unknown[], first: string) {
    const queryFn = vi
      .fn()
      .mockResolvedValueOnce(first)
      .mockImplementation(() => new Promise<string>(() => {}));
    const observer = new QueryObserver(client, { queryKey, queryFn, staleTime: Infinity });
    const unsubscribe = observer.subscribe(() => {});
    return { queryFn, unsubscribe };
  }

  it("drops the panel's answer read as A once list stats read as B", async () => {
    const { client, unbind } = await setup();
    const panelKey = ["github-info", "conv", SPOKE_PR, ...macReadScopeKey("gitspoke", SPOKE)];
    expect(acceptMacAnswer(macReadTicket("gitspoke", SPOKE), panelAnswer("alice"))).toBe(
      "accepted",
    );
    const panel = shown(client, panelKey, "panel as alice");
    await vi.waitFor(() => expect(client.getQueryData(panelKey)).toBe("panel as alice"));
    // A read still in flight when the reader changes isn't cancelled.
    let answerInFlight: (value: string) => void = () => {};
    const inFlightKey = ["diff", SPOKE_PR, ...macReadScopeKey("gitspoke", SPOKE)];
    const inFlight = new QueryObserver(client, {
      queryKey: inFlightKey,
      queryFn: () =>
        new Promise<string>((resolve) => {
          answerInFlight = resolve;
        }),
    });
    const stopInFlight = inFlight.subscribe(() => {});

    expect(acceptMacRead(beginMacRead(macReadScopeKey("gitspoke", SPOKE)), "bob")).toBe(
      "transition",
    );

    await vi.waitFor(() => expect(panel.queryFn).toHaveBeenCalledTimes(2));
    expect(client.getQueryData(panelKey)).toBeUndefined();
    answerInFlight("diff as bob");
    await vi.waitFor(() => expect(client.getQueryData(inFlightKey)).toBe("diff as bob"));
    panel.unsubscribe();
    stopInFlight();
    unbind();
  });

  it.each([
    ["fails", (settle: Settle) => settle.reject(new Error("rate_limited")), undefined],
    ["answers as B", (settle: Settle) => settle.resolve("panel as bob"), "panel as bob"],
  ])(
    "hides A's panel answer at once while its refetch runs, and after the refetch %s",
    async (_name, release, expected) => {
      const { client, unbind } = await setup();
      const panelKey = ["github-info", "conv", SPOKE_PR, ...macReadScopeKey("gitspoke", SPOKE)];
      acceptMacAnswer(macReadTicket("gitspoke", SPOKE), panelAnswer("alice"));
      const settle: Settle = { resolve: () => {}, reject: () => {} };
      const queryFn = vi
        .fn()
        .mockResolvedValueOnce("panel as alice")
        .mockImplementation(
          () =>
            new Promise<string>((resolve, reject) => {
              settle.resolve = resolve;
              settle.reject = reject;
            }),
        );
      const observer = new QueryObserver(client, { queryKey: panelKey, queryFn });
      const seen: unknown[] = [];
      const unsubscribe = observer.subscribe((result) => seen.push(result.data));
      await vi.waitFor(() => expect(client.getQueryData(panelKey)).toBe("panel as alice"));
      // A poll starts and is held, still showing A's answer.
      void observer.refetch();
      await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2));
      expect(observer.getCurrentResult().data).toBe("panel as alice");

      expect(acceptMacRead(beginMacRead(macReadScopeKey("gitspoke", SPOKE)), "bob")).toBe(
        "transition",
      );

      // Gone before the held read settles, and that read isn't cancelled or repeated.
      expect(client.getQueryData(panelKey)).toBeUndefined();
      expect(observer.getCurrentResult().data).toBeUndefined();
      seen.length = 0;
      release(settle);
      await vi.waitFor(() => expect(observer.getCurrentResult().fetchStatus).toBe("idle"));
      expect(client.getQueryData(panelKey)).toBe(expected);
      expect(seen).not.toContain("panel as alice");
      expect(queryFn).toHaveBeenCalledTimes(2);
      unsubscribe();
      unbind();
    },
  );

  describe("when the held refetch is cancelled, which rolls back to A", () => {
    /** A panel answered as A whose next read, which uses its abort signal, is held. */
    async function heldAfterA() {
      const { client, unbind } = await setup();
      const panelKey = ["github-info", "conv", SPOKE_PR, ...macReadScopeKey("gitspoke", SPOKE)];
      acceptMacAnswer(macReadTicket("gitspoke", SPOKE), panelAnswer("alice"));
      const queryFn = vi
        .fn()
        .mockResolvedValueOnce("panel as alice")
        .mockImplementation(
          ({ signal }: { signal: AbortSignal }) =>
            new Promise<string>((_resolve, reject) => {
              signal.addEventListener("abort", () => reject(new Error("aborted")));
            }),
        );
      const options = { queryKey: panelKey, queryFn };
      const observer = new QueryObserver(client, options);
      const seen: unknown[] = [];
      // As a component subscribes: notified once query-core's batch has settled.
      const unsubscribe = observer.subscribe(
        notifyManager.batchCalls(() => seen.push(observer.getCurrentResult().data)),
      );
      await vi.waitFor(() => expect(client.getQueryData(panelKey)).toBe("panel as alice"));
      void observer.refetch();
      await vi.waitFor(() => expect(queryFn).toHaveBeenCalledTimes(2));
      expect(acceptMacRead(beginMacRead(macReadScopeKey("gitspoke", SPOKE)), "bob")).toBe(
        "transition",
      );
      expect(client.getQueryData(panelKey)).toBeUndefined();
      seen.length = 0;
      return { client, unbind, panelKey, options, observer, queryFn, seen, unsubscribe };
    }

    it("keeps A gone after a mutation cancels the read", async () => {
      const held = await heldAfterA();
      await held.client.cancelQueries({ queryKey: held.panelKey });
      // Rolled back to A, and dropped again before anything shows it.
      expect(held.client.getQueryData(held.panelKey)).toBeUndefined();
      expect(held.observer.getCurrentResult().data).toBeUndefined();
      await vi.waitFor(() => expect(held.queryFn).toHaveBeenCalledTimes(3));
      expect(held.client.getQueryData(held.panelKey)).toBeUndefined();
      expect(held.observer.getCurrentResult().data).toBeUndefined();
      expect(held.seen).not.toContain("panel as alice");
      held.unsubscribe();
      held.unbind();
    });

    it("keeps A gone when the panel closes mid-read and opens again", async () => {
      const held = await heldAfterA();
      // The last observer leaving cancels the read that used its signal.
      held.unsubscribe();
      await vi.waitFor(() =>
        expect(held.client.getQueryCache().find({ queryKey: held.panelKey })).toBeUndefined(),
      );
      const reopened = new QueryObserver(held.client, held.options);
      const seen: unknown[] = [];
      const stop = reopened.subscribe(
        notifyManager.batchCalls(() => seen.push(reopened.getCurrentResult().data)),
      );
      await vi.waitFor(() => expect(held.queryFn).toHaveBeenCalledTimes(3));
      expect(reopened.getCurrentResult().data).toBeUndefined();
      expect(held.client.getQueryData(held.panelKey)).toBeUndefined();
      expect(seen).not.toContain("panel as alice");
      stop();
      held.unbind();
    });
  });

  it("drops list stats read as A once a panel read answers as B", async () => {
    const { client, unbind } = await setup();
    const statsKey = [...macReadScopeKey("gitspoke", SPOKE), "pull_request_list_stats", "batch"];
    expect(acceptMacRead(beginMacRead(macReadScopeKey("gitspoke", SPOKE)), "alice")).toBe(
      "accepted",
    );
    const stats = shown(client, statsKey, "stats as alice");
    await vi.waitFor(() => expect(client.getQueryData(statsKey)).toBe("stats as alice"));

    expect(acceptMacAnswer(macReadTicket("gitspoke", SPOKE), panelAnswer("bob"))).toBe("accepted");

    await vi.waitFor(() => expect(stats.queryFn).toHaveBeenCalledTimes(2));
    expect(client.getQueryData(statsKey)).toBeUndefined();
    stats.unsubscribe();
    unbind();
  });
});

describe("bindMacReadScopes and a PR's shared records", () => {
  const scope = macReadScopeKey("github", "github.com");
  const record = (reader: string | null, epoch: number) => [
    "github-info",
    "pr",
    "https://github.com/acme/app/pull/7",
    ...scope,
    "reader",
    reader,
    epoch,
  ];

  afterEach(() => resetMacReadScopesForTests());

  it("resets a past epoch's record without reading it again, and drops one nothing shows", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const unbind = bindMacReadScopes(client);
    acceptMacRead(beginMacRead(scope), "alice");
    const shown = record("alice", macReadEpoch(scope));
    const unused = record("alice", macReadEpoch(scope));
    unused[2] = "https://github.com/acme/app/pull/8";
    client.setQueryData(shown, "as alice");
    client.setQueryData(unused, "as alice");
    const read = vi.fn(async () => "read again");
    const stop = new QueryObserver(client, {
      queryKey: shown,
      queryFn: read,
      staleTime: Infinity,
    }).subscribe(() => {});

    // Bob's answer moves the scope to a new epoch: its views read under Bob's record instead.
    acceptMacRead(beginMacRead(scope), "bob");

    expect(client.getQueryData(shown)).toBeUndefined();
    expect(client.getQueryCache().find({ queryKey: unused, exact: true })).toBeUndefined();
    expect(read).not.toHaveBeenCalled();
    stop();
    unbind();
  });

  it("keeps a refused first read's error on screen, in its unmoved epoch", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const unbind = bindMacReadScopes(client);
    const bootstrap = record(null, macReadEpoch(scope));
    const refused = new Error("Sign in again.");
    const stop = new QueryObserver(client, {
      queryKey: bootstrap,
      queryFn: async () => {
        throw refused;
      },
    }).subscribe(() => {});
    await vi.waitFor(() => expect(client.getQueryState(bootstrap)?.status).toBe("error"));

    refuseMacReadScope(scope);
    await new Promise((resolve) => {
      setTimeout(resolve, 0);
    });

    expect(client.getQueryState(bootstrap)?.error).toBe(refused);
    expect(record(null, macReadEpoch(scope))).toEqual(bootstrap);
    stop();
    unbind();
  });
});
