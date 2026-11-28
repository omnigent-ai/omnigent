import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PullRequestAssociation } from "@/newPullRequestPage/hooks/usePullRequests";
import type { ServerInfo } from "@/lib/capabilities";
import { pullRequestKey } from "@/lib/pullRequestReference";
import {
  SESSION_PULL_REQUEST_LIMIT_REASON,
  sessionPullRequestMembership,
} from "@/lib/sessionPullRequestMembership";
import {
  linkPullRequestAttempt,
  type PullRequestLinkOutcome,
  usePullRequestAttachStore,
} from "@/store/pullRequestAttach";
import {
  hasPullRequestIntents,
  LINK_UNCONFIRMED_REASON,
  LINK_UNREADABLE_REASON,
  PULL_REQUEST_INTENT_LIFETIME_MS,
  resetPullRequestIntentsForTests,
  UNLINK_UNREADABLE_REASON,
  useSessionPullRequestIntentStore,
} from "@/store/sessionPullRequestIntents";

vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ session_pull_requests: true }),
}));

import { linkFailureIsAmbiguous, useAttachSessionPullRequest } from "./useAttachSessionPullRequest";
import { pullRequestRecordKey, sessionRecordOwner } from "@/lib/pullRequestRecord";
import { useSetPullRequestPreference } from "../newPullRequestPage/hooks/usePullRequests";
import {
  fetchSessionPullRequests,
  sessionPullRequestsKey,
  startSessionUnlinkRun,
  type StoredPullRequests,
  UNLINK_OVERTAKEN_REASON,
  useUnlinkSessionPr,
} from "../newPullRequestPage/hooks/useSessionPullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const SESSION = "conv_1";
const url = (number: number) => `https://github.com/acme/app/pull/${number}`;
const row = (number: number): PullRequestAssociation => ({
  url: url(number),
  host: "github.com",
  repository: "acme/app",
  number,
  relationship: "attached",
});

/** The server as these tests see it: its stored list, and how the link request answers. */
const server = {
  stored: [] as PullRequestAssociation[],
  listReads: 0,
  /** Holds the next list read's answer back until the test releases it. */
  holdListRead: null as Promise<void> | null,
  failListReads: false,
  /** Holds the next unlink's answer back, after the server has applied it. */
  holdUnlink: null as Promise<void> | null,
  refuseUnlink: false,
  /** After applying the unlink, lose its answer ("lost") or answer with this status. */
  unlinkAnswerFails: null as "lost" | number | null,
  /** How a preference save answers; it's read only when a test saves one. */
  answerPreference: (): Promise<Response> => Promise.reject(new Error("unset")),
  answerLink: (_url: string): Promise<Response> => Promise.reject(new Error("unset")),
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function deferred<Value>() {
  let resolve: (value: Value) => void = () => {};
  const promise = new Promise<Value>((onResolve) => {
    resolve = onResolve;
  });
  return { promise, resolve };
}

function renderHooks(client: QueryClient) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => ({
      attach: useAttachSessionPullRequest(SESSION),
      unlink: useUnlinkSessionPr(SESSION),
      preference: useSetPullRequestPreference(SESSION),
    }),
    { wrapper },
  ).result;
}

/** The session's list as its own reads last wrote it. */
const cached = (client: QueryClient) =>
  client.getQueryData<StoredPullRequests>(sessionPullRequestsKey(SESSION))?.prs.map((pr) => pr.url);
const keysOf = (...numbers: number[]) =>
  numbers.map((number) => pullRequestKey(url(number))).sort();
/** What every PR surface shows: the list, with the pending links and unlinks over it. */
const shown = (client: QueryClient) =>
  [
    ...sessionPullRequestMembership({
      stored: client.getQueryData<StoredPullRequests>(sessionPullRequestsKey(SESSION)),
      live: undefined,
      intents: useSessionPullRequestIntentStore.getState().bySession[SESSION],
    }).keys,
  ].sort();
const unconfirmed = (number: number) =>
  useSessionPullRequestIntentStore.getState().unconfirmed[SESSION]?.[pullRequestKey(url(number))];
const settled = () => waitFor(() => expect(hasPullRequestIntents(SESSION)).toBe(false));
/** A list read that starts now and answers, with what the server held then, when released. */
function startHeldRead(client: QueryClient) {
  const release = deferred<undefined>();
  server.holdListRead = release.promise;
  void client
    .fetchQuery({
      queryKey: sessionPullRequestsKey(SESSION),
      queryFn: ({ signal }) => fetchSessionPullRequests(SESSION, signal),
      staleTime: 0,
    })
    .catch(() => {});
  return () => release.resolve(undefined);
}
const lostAfterLinking = (number: number) => async () => {
  server.stored = [...server.stored, row(number)];
  throw new TypeError("Failed to fetch");
};
const answersWithServerList = (number: number) => async () => {
  server.stored = [...server.stored, row(number)];
  return json({ prs: server.stored, selected_pr_url: url(number) });
};

const clients: QueryClient[] = [];

function newClient(stored: PullRequestAssociation[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(client);
  client.setQueryData<StoredPullRequests>(sessionPullRequestsKey(SESSION), {
    prs: stored,
    synced: true,
  });
  return client;
}

beforeEach(() => {
  server.stored = [row(1)];
  server.listReads = 0;
  server.holdListRead = null;
  server.failListReads = false;
  server.holdUnlink = null;
  server.refuseUnlink = false;
  server.unlinkAnswerFails = null;
  resetPullRequestIntentsForTests();
  usePullRequestAttachStore.setState({ attempts: {}, drafts: {}, lastAttached: null });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input), "http://localhost").pathname;
      if (path.endsWith("/preferences")) return server.answerPreference();
      if (init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { url: string };
        return server.answerLink(body.url);
      }
      if (path.endsWith("/pull-requests") && init?.method === "DELETE") {
        if (server.refuseUnlink) return json({ detail: "You can't unlink that here" }, 403);
        const removed = new URL(String(input), "http://localhost").searchParams.get("url");
        server.stored = server.stored.filter((pr) => pr.url !== removed);
        const snapshot = [...server.stored];
        const hold = server.holdUnlink;
        server.holdUnlink = null;
        if (hold) await hold;
        if (server.unlinkAnswerFails === "lost") throw new TypeError("Failed to fetch");
        if (server.unlinkAnswerFails !== null) {
          return json({ detail: "Upstream trouble" }, server.unlinkAnswerFails);
        }
        return json({ object: "list", data: snapshot, has_more: false, synced: true });
      }
      if (path.endsWith("/pull-requests")) {
        server.listReads += 1;
        // What the server holds when the read arrives, not when it's answered.
        const snapshot = [...server.stored];
        const hold = server.holdListRead;
        server.holdListRead = null;
        if (hold) await hold;
        if (server.failListReads) return json({ detail: "List unavailable" }, 503);
        return json({
          object: "list",
          data: snapshot,
          has_more: false,
          synced: true,
          first_id: null,
          last_id: null,
        });
      }
      return json({}, 404);
    }),
  );
});

afterEach(async () => {
  vi.useRealTimers();
  // A test's reads still out never settle the next test's intents for the same session.
  const used = clients.splice(0);
  await Promise.all(used.map((client) => client.cancelQueries()));
  for (const client of used) client.clear();
  vi.unstubAllGlobals();
});

describe("useAttachSessionPullRequest", () => {
  it("shows the link at once, then settles it by the list's own read", async () => {
    const client = newClient([row(1)]);
    const answer = deferred<Response>();
    server.answerLink = () => answer.promise;
    const hooks = renderHooks(client);
    let linking: Promise<string | undefined> = Promise.resolve(undefined);
    act(() => {
      linking = hooks.current.attach.mutateAsync(url(2));
    });
    await waitFor(() => expect(shown(client)).toEqual(keysOf(1, 2)));
    expect(cached(client)).toEqual([url(1)]);
    server.stored = [row(1), row(2)];
    await act(async () => {
      answer.resolve(json({ prs: server.stored, selected_pr_url: url(2) }));
      await expect(linking).resolves.toBe(url(2));
    });
    await settled();
    expect(cached(client)).toEqual([url(1), url(2)]);
  });

  it("never writes the list from the link's reply", async () => {
    const client = newClient([row(1)]);
    server.answerLink = async () => {
      server.stored = [row(1), row(2)];
      return json({ prs: [row(1), row(2), row(9)], selected_pr_url: url(2) });
    };
    const hooks = renderHooks(client);
    await act(async () => {
      await hooks.current.attach.mutateAsync(url(2));
    });
    await settled();
    expect(cached(client)).toEqual([url(1), url(2)]);
  });

  it("calls a link linked when its answer was lost but the server's list has it", async () => {
    const client = newClient([row(1)]);
    server.answerLink = lostAfterLinking(2);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).resolves.toBe(url(2));
    });
    expect(server.listReads).toBe(1);
    expect(shown(client)).toEqual(keysOf(1, 2));
  });

  it("reports a server failure once the list, read twice, still lacks the PR", async () => {
    const client = newClient([row(1)]);
    server.answerLink = async () => json({ detail: "Upstream timed out" }, 502);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow("Upstream timed out");
    });
    expect(server.listReads).toBe(2);
    expect(shown(client)).toEqual(keysOf(1));
    expect(unconfirmed(2)).toBe("Upstream timed out");
  });

  it("explains the limit when the server has no room for another PR", async () => {
    const client = newClient([row(1)]);
    server.answerLink = async () =>
      json({ detail: "A session can link at most 50 pull requests" }, 409);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow(
        SESSION_PULL_REQUEST_LIMIT_REASON,
      );
    });
  });

  it("keeps a PR another link won the last room for out, and the winner in", async () => {
    const client = newClient([row(1)]);
    // Both started with room; the other link took the last place first.
    server.answerLink = async () => {
      server.stored = [row(1), row(3)];
      return json({ detail: "A session can link at most 50 pull requests" }, 409);
    };
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow(
        SESSION_PULL_REQUEST_LIMIT_REASON,
      );
    });
    expect(cached(client)).toEqual([url(1), url(3)]);
    expect(shown(client)).toEqual(keysOf(1, 3));
  });

  it("reverts at once on a refusal the request caused, without reading the list again", async () => {
    const client = newClient([row(1)]);
    server.answerLink = async () => json({ detail: "The host can't read that PR" }, 400);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow(
        "The host can't read that PR",
      );
    });
    expect(server.listReads).toBe(0);
    expect(shown(client)).toEqual(keysOf(1));
  });

  it("writes no PR record from the link's reply", async () => {
    const client = newClient([row(1)]);
    const own = pullRequestRecordKey("pr-page-github-info", url(2), sessionRecordOwner(SESSION));
    const newer = { prs: [row(1)], selected_pr_url: url(1), pr: { title: "Newer read" } };
    client.setQueryData(own, newer);
    server.answerLink = async () => {
      server.stored = [row(1), row(2)];
      return json({ prs: server.stored, selected_pr_url: url(2), pr: { title: "Old" } });
    };
    const hooks = renderHooks(client);
    await act(async () => {
      await hooks.current.attach.mutateAsync(url(2));
    });
    expect(client.getQueryData(own)).toBe(newer);
  });
});

describe("useAttachSessionPullRequest, settled by reads only (review repros)", () => {
  it("r1: settles a lost answer by a read sent after it, not one that began before the link", async () => {
    const client = newClient([row(1)]);
    const releaseOlder = startHeldRead(client);
    server.answerLink = lostAfterLinking(2);
    const hooks = renderHooks(client);
    let outcome: Promise<string | undefined> = Promise.resolve(undefined);
    act(() => {
      outcome = hooks.current.attach.mutateAsync(url(2));
    });
    setTimeout(releaseOlder, 50);
    await act(async () => {
      await expect(outcome).resolves.toBe(url(2));
    });
    expect(cached(client)).toEqual([url(1), url(2)]);
  });

  it("r1: doesn't take an older read's word that the PR is linked once another client unlinked it", async () => {
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const releaseOlder = startHeldRead(client);
    server.answerLink = async () => {
      server.stored = [row(1)];
      throw new TypeError("Failed to fetch");
    };
    const hooks = renderHooks(client);
    let outcome: Promise<string | undefined> = Promise.resolve(undefined);
    act(() => {
      outcome = hooks.current.attach.mutateAsync(url(2));
    });
    setTimeout(releaseOlder, 50);
    await act(async () => {
      await expect(outcome).rejects.toThrow("Failed to fetch");
    });
    expect(shown(client)).toEqual(keysOf(1));
  });

  it("r1: says it couldn't confirm when the list can't be read after a lost answer", async () => {
    const client = newClient([row(1)]);
    server.failListReads = true;
    server.answerLink = async () => {
      throw new TypeError("Failed to fetch");
    };
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow(
        LINK_UNREADABLE_REASON,
      );
    });
    expect(shown(client)).toEqual(keysOf(1));
  });

  it("r1: reverts a link another window unlinked while it ran, by the reads after its reply", async () => {
    const client = newClient([row(1)]);
    const answer = deferred<Response>();
    server.answerLink = () => answer.promise;
    const hooks = renderHooks(client);
    let outcome: Promise<string | undefined> = Promise.resolve(undefined);
    act(() => {
      outcome = hooks.current.attach.mutateAsync(url(2));
    });
    // Linked, then unlinked from another window before this reply came back.
    server.stored = [row(1)];
    await act(async () => {
      answer.resolve(json({ prs: [row(1), row(2)], selected_pr_url: url(2) }));
      await expect(outcome).rejects.toThrow(LINK_UNCONFIRMED_REASON);
    });
    await settled();
    expect(cached(client)).toEqual([url(1)]);
    expect(shown(client)).toEqual(keysOf(1));
    expect(unconfirmed(2)).toBe(LINK_UNCONFIRMED_REASON);
  });

  it("r2: lets an unlink that finished while the link was being settled decide the list", async () => {
    const client = newClient([row(1)]);
    const settlingRead = deferred<undefined>();
    server.answerLink = async () => {
      // The link lands on the server, but its answer is lost; the read after it is held.
      server.stored = [row(1), row(2)];
      server.holdListRead = settlingRead.promise;
      throw new TypeError("Failed to fetch");
    };
    const hooks = renderHooks(client);
    let outcome: Promise<PullRequestLinkOutcome> = Promise.resolve({ status: "busy" });
    act(() => {
      outcome = linkPullRequestAttempt(SESSION, url(2), hooks.current.attach.mutateAsync);
    });
    await waitFor(() => expect(server.listReads).toBe(1));
    await act(async () => {
      await hooks.current.unlink.mutateAsync(url(2));
    });
    settlingRead.resolve(undefined);
    // The overtaken link says nothing: no Linked, no selection, no toast to follow.
    await act(async () => {
      await expect(outcome).resolves.toEqual({ status: "overtaken" });
    });
    expect(usePullRequestAttachStore.getState().lastAttached).toBeNull();
    expect(usePullRequestAttachStore.getState().attempts[SESSION]).toBeUndefined();
    await settled();
    expect(cached(client)).toEqual([url(1)]);
    expect(shown(client)).toEqual(keysOf(1));
  });

  it("r2: writes nothing from a successful answer once an unlink started after the link", async () => {
    const client = newClient([row(1)]);
    const answer = deferred<Response>();
    server.answerLink = () => answer.promise;
    const hooks = renderHooks(client);
    let outcome: Promise<PullRequestLinkOutcome> = Promise.resolve({ status: "busy" });
    act(() => {
      outcome = linkPullRequestAttempt(SESSION, url(2), hooks.current.attach.mutateAsync);
    });
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    server.stored = [row(1), row(2)];
    await act(async () => {
      await hooks.current.unlink.mutateAsync(url(2));
    });
    await act(async () => {
      answer.resolve(json({ prs: [row(1), row(2)], selected_pr_url: url(2) }));
      await expect(outcome).resolves.toEqual({ status: "overtaken" });
    });
    expect(usePullRequestAttachStore.getState().lastAttached).toBeNull();
    await settled();
    expect(cached(client)).toEqual([url(1)]);
    expect(
      client
        .getQueryData<{ prs: PullRequestAssociation[] }>(["pr-page-github-info", SESSION])
        ?.prs.map((pr) => pr.url) ?? [],
    ).not.toContain(url(2));
  });

  it("r3: keeps a link confirmed after an unlink started, when the unlink's answer comes last", async () => {
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const unlinkAnswer = deferred<undefined>();
    server.holdUnlink = unlinkAnswer.promise;
    server.answerLink = answersWithServerList(3);
    const hooks = renderHooks(client);
    let unlinking: Promise<unknown> = Promise.resolve();
    act(() => {
      unlinking = hooks.current.unlink.mutateAsync(url(2));
    });
    // The server has removed #2, but its answer, saying [1], is still on its way.
    await waitFor(() => expect(server.stored.map((pr) => pr.url)).toEqual([url(1)]));
    expect(shown(client)).toEqual(keysOf(1));
    await act(async () => {
      await hooks.current.attach.mutateAsync(url(3));
    });
    expect(shown(client)).toEqual(keysOf(1, 3));
    unlinkAnswer.resolve(undefined);
    await act(async () => {
      await unlinking;
    });
    await settled();
    expect(server.stored.map((pr) => pr.url)).toEqual([url(1), url(3)]);
    expect(cached(client)).toEqual([url(1), url(3)]);
  });

  it("r3: keeps a PR linked again after its unlink started, when the unlink's answer comes last", async () => {
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const unlinkAnswer = deferred<undefined>();
    server.holdUnlink = unlinkAnswer.promise;
    server.answerLink = answersWithServerList(2);
    const hooks = renderHooks(client);
    let unlinking: Promise<string> = Promise.resolve("");
    act(() => {
      // Caught now: it ends while the link is still being awaited.
      unlinking = hooks.current.unlink.mutateAsync(url(2)).then(
        () => "removed",
        (error: Error) => error.message,
      );
    });
    await waitFor(() => expect(server.stored.map((pr) => pr.url)).toEqual([url(1)]));
    await act(async () => {
      await hooks.current.attach.mutateAsync(url(2));
    });
    unlinkAnswer.resolve(undefined);
    // The overtaken unlink says it wasn't removed, so nothing moves off the PR.
    await act(async () => {
      await expect(unlinking).resolves.toBe(UNLINK_OVERTAKEN_REASON);
    });
    await settled();
    expect(cached(client)).toEqual([url(1), url(2)]);
    expect(shown(client)).toEqual(keysOf(1, 2));
  });

  it("r3: lets a Remove All's late answer leave a newer single link of the same PR shown", async () => {
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const deleteAnswer = deferred<undefined>();
    server.holdUnlink = deleteAnswer.promise;
    server.answerLink = answersWithServerList(2);
    const hooks = renderHooks(client);
    // The batch runs as on main, but its answers write nothing; only its closing read does.
    const run = startSessionUnlinkRun(
      client,
      { session_pull_requests: true } as ServerInfo,
      SESSION,
      [url(2)],
    );
    await waitFor(() => expect(server.stored.map((pr) => pr.url)).toEqual([url(1)]));
    await act(async () => {
      await hooks.current.attach.mutateAsync(url(2));
    });
    expect(cached(client)).toEqual([url(1), url(2)]);
    // The old answer lands while the run's closing read is still out: it changes nothing.
    const closingRead = deferred<undefined>();
    server.holdListRead = closingRead.promise;
    const reads = server.listReads;
    deleteAnswer.resolve(undefined);
    await waitFor(() => expect(server.listReads).toBe(reads + 1));
    expect(cached(client)).toEqual([url(1), url(2)]);
    expect(shown(client)).toEqual(keysOf(1, 2));
    closingRead.resolve(undefined);
    await act(async () => {
      await run;
    });
    expect(server.stored.map((pr) => pr.url)).toEqual([url(1), url(2)]);
    expect(cached(client)).toEqual([url(1), url(2)]);
    expect(shown(client)).toEqual(keysOf(1, 2));
  });

  it("r4: lets a newer read that saw another window's unlink win over a held one", async () => {
    const client = newClient([row(1)]);
    const heldRead = deferred<undefined>();
    server.answerLink = async () => {
      // The link lands but its answer is lost; the read that settles it sees [1, 2], held.
      server.stored = [row(1), row(2)];
      server.holdListRead = heldRead.promise;
      throw new TypeError("Failed to fetch");
    };
    const hooks = renderHooks(client);
    let outcome: Promise<string> = Promise.resolve("");
    act(() => {
      // Caught now: it ends while the test is still releasing the held read.
      outcome = hooks.current.attach.mutateAsync(url(2)).then(
        () => "linked",
        (error: Error) => error.message,
      );
    });
    await waitFor(() => expect(server.listReads).toBe(1));
    // Another window unlinks #2; a normal read of the list here sees that.
    server.stored = [row(1)];
    await act(async () => {
      await client.refetchQueries({ queryKey: sessionPullRequestsKey(SESSION), exact: true });
    });
    heldRead.resolve(undefined);
    await act(async () => {
      await expect(outcome).resolves.toBe("Failed to fetch");
    });
    expect(server.stored.map((pr) => pr.url)).toEqual([url(1)]);
    expect(cached(client)).toEqual([url(1)]);
    expect(shown(client)).toEqual(keysOf(1));
  });

  it("keeps the session's list as its reads left it when a preference reply lists an older one", async () => {
    const client = newClient([row(1)]);
    client.setQueryData(["pr-page-github-info", SESSION], { prs: [row(1), row(2)] });
    server.answerPreference = async () => json({ prs: [row(1)], account: "octo" });
    const hooks = renderHooks(client);
    await act(async () => {
      await hooks.current.preference.mutateAsync({ remote: "origin" });
    });
    expect(
      client
        .getQueryData<{ prs: PullRequestAssociation[] }>(["pr-page-github-info", SESSION])
        ?.prs.map((pr) => pr.url),
    ).toEqual([url(1), url(2)]);
  });
});

describe("useUnlinkSessionPr", () => {
  it("shows the PR unlinked at once and drops its row by the list's read", async () => {
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const unlinkAnswer = deferred<undefined>();
    server.holdUnlink = unlinkAnswer.promise;
    const hooks = renderHooks(client);
    let unlinking: Promise<unknown> = Promise.resolve();
    act(() => {
      unlinking = hooks.current.unlink.mutateAsync(url(2));
    });
    await waitFor(() => expect(shown(client)).toEqual(keysOf(1)));
    expect(cached(client)).toEqual([url(1), url(2)]);
    unlinkAnswer.resolve(undefined);
    await act(async () => {
      await unlinking;
    });
    expect(cached(client)).toEqual([url(1)]);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
  });

  it("brings the PR back at once when the unlink is refused", async () => {
    server.stored = [row(1), row(2)];
    server.refuseUnlink = true;
    const client = newClient([row(1), row(2)]);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.unlink.mutateAsync(url(2))).rejects.toThrow(
        "You can't unlink that here",
      );
    });
    expect(shown(client)).toEqual(keysOf(1, 2));
    expect(cached(client)).toEqual([url(1), url(2)]);
  });
});

describe("intent lifecycle (review supplemental cases)", () => {
  it.each([
    ["a lost answer", "lost" as const],
    ["a 408", 408],
    ["a 409", 409],
    ["a 502", 502],
  ])("settles an unlink that committed before %s by a read of the list", async (_case, failure) => {
    server.stored = [row(1), row(2)];
    server.unlinkAnswerFails = failure;
    const client = newClient([row(1), row(2)]);
    const hooks = renderHooks(client);
    await act(async () => {
      await hooks.current.unlink.mutateAsync(url(2));
    });
    expect(server.listReads).toBeGreaterThan(0);
    expect(cached(client)).toEqual([url(1)]);
    expect(shown(client)).toEqual(keysOf(1));
    expect(hasPullRequestIntents(SESSION)).toBe(false);
  });

  it("still reports an unclear unlink the list shows didn't go through, and brings it back", async () => {
    server.stored = [row(1), row(2)];
    server.unlinkAnswerFails = "lost";
    server.failListReads = true;
    const client = newClient([row(1), row(2)]);
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.unlink.mutateAsync(url(2))).rejects.toThrow(
        UNLINK_UNREADABLE_REASON,
      );
    });
    expect(shown(client)).toEqual(keysOf(1, 2));
  });

  it("drops a link the server answered once the list can't be read, saying it couldn't confirm", async () => {
    const client = newClient([row(1)]);
    // Another client already unlinked #2 again, and the list can't be read to say so.
    server.answerLink = async () => json({ prs: [row(1), row(2)], selected_pr_url: url(2) });
    server.failListReads = true;
    const hooks = renderHooks(client);
    await act(async () => {
      await expect(hooks.current.attach.mutateAsync(url(2))).rejects.toThrow(
        LINK_UNREADABLE_REASON,
      );
    });
    expect(server.listReads).toBe(2);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
    expect(shown(client)).toEqual(keysOf(1));
    expect(unconfirmed(2)).toBe(LINK_UNREADABLE_REASON);
  });

  it("ends an unlink whose answer never comes once its time is up, freeing the removal queue", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    server.stored = [row(1), row(2)];
    server.holdUnlink = new Promise<void>(() => {});
    const client = newClient([row(1), row(2)]);
    const hooks = renderHooks(client);
    let outcome: Promise<string> = Promise.resolve("");
    act(() => {
      outcome = hooks.current.unlink.mutateAsync(url(2)).then(
        () => "removed",
        (error: Error) => error.message,
      );
    });
    await waitFor(() => expect(server.stored.map((pr) => pr.url)).toEqual([url(1)]));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_INTENT_LIFETIME_MS + 1);
    });
    await expect(outcome).resolves.toBe(UNLINK_UNREADABLE_REASON);
    await waitFor(() => expect(hooks.current.unlink.isPending).toBe(false));
    // The shared removal queue moves on: the next unlink runs and settles.
    await act(async () => {
      await hooks.current.unlink.mutateAsync(url(1));
    });
    expect(cached(client)).toEqual([]);
  });

  it("ends an unlink whose confirming read never answers once its time is up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    server.stored = [row(1), row(2)];
    const client = newClient([row(1), row(2)]);
    const hooks = renderHooks(client);
    server.holdListRead = new Promise<void>(() => {});
    let outcome: Promise<string> = Promise.resolve("");
    act(() => {
      outcome = hooks.current.unlink.mutateAsync(url(2)).then(
        () => "removed",
        (error: Error) => error.message,
      );
    });
    await waitFor(() => expect(server.listReads).toBe(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_INTENT_LIFETIME_MS + 1);
    });
    await expect(outcome).resolves.toBe(UNLINK_UNREADABLE_REASON);
    await waitFor(() => expect(hooks.current.unlink.isPending).toBe(false));
  });

  it("ends a link whose request never answers once its time is up, and frees the session", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = newClient([row(1)]);
    server.answerLink = () => new Promise<Response>(() => {});
    const hooks = renderHooks(client);
    let outcome: Promise<PullRequestLinkOutcome> = Promise.resolve({ status: "busy" });
    act(() => {
      outcome = linkPullRequestAttempt(SESSION, url(2), hooks.current.attach.mutateAsync);
    });
    await waitFor(() => expect(shown(client)).toEqual(keysOf(1, 2)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_INTENT_LIFETIME_MS);
    });
    await expect(outcome).resolves.toEqual({ status: "failed", message: LINK_UNREADABLE_REASON });
    expect(shown(client)).toEqual(keysOf(1));
    // The session's one link at a time is free again.
    expect(usePullRequestAttachStore.getState().attempts[SESSION]?.status).toBe("failed");
  });

  it("ends a link whose confirming read never answers once its time is up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const client = newClient([row(1)]);
    server.answerLink = async () => {
      server.stored = [row(1), row(2)];
      server.holdListRead = new Promise<void>(() => {});
      return json({ prs: server.stored, selected_pr_url: url(2) });
    };
    const hooks = renderHooks(client);
    let outcome: Promise<string | undefined> = Promise.resolve(undefined);
    act(() => {
      outcome = hooks.current.attach.mutateAsync(url(2)).catch((error: Error) => error.message);
    });
    await waitFor(() => expect(server.listReads).toBe(1));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(PULL_REQUEST_INTENT_LIFETIME_MS);
    });
    await expect(outcome).resolves.toBe(LINK_UNREADABLE_REASON);
    expect(hasPullRequestIntents(SESSION)).toBe(false);
  });
});

describe("linkFailureIsAmbiguous", () => {
  it.each([
    [new TypeError("Failed to fetch"), true],
    [Object.assign(new Error("conflict"), { status: 409 }), true],
    [Object.assign(new Error("gateway"), { status: 504 }), true],
    [Object.assign(new Error("bad request"), { status: 400 }), false],
    [Object.assign(new Error("forbidden"), { status: 403 }), false],
  ])("%s → %s", (error, ambiguous) => {
    expect(linkFailureIsAmbiguous(error)).toBe(ambiguous);
  });
});
