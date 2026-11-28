import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import type * as AnnounceChange from "@/components/pendingChange/announceChange";
import { pullRequestKey } from "@/lib/pullRequestReference";
import { resetBulkRunsForTests, runningBulkRuns, stopBulkRun } from "@/pullRequestList/bulkRuns";
import {
  sessionPullRequestsKey,
  startSessionUnlinkRun,
  startTreeUnlinkRun,
  useSessionRemovalProgress,
  useSessionUnlinkPending,
  useUnlinkSessionPr,
} from "./useSessionPullRequests";
import { ANNOUNCE_REFILL_DELAY_MS } from "@/components/pendingChange/announceChange";

// Every announcement the runs make, as they ask for it; the live region still receives each.
const announced = vi.hoisted(() => [] as string[]);
vi.mock("@/components/pendingChange/announceChange", async (importOriginal) => {
  const actual = await importOriginal<typeof AnnounceChange>();
  return {
    ...actual,
    announceChange: (message: string) => {
      announced.push(message);
      actual.announceChange(message);
    },
  };
});

const urls = [1, 2, 3].map((number) => `https://github.com/example/app/pull/${number}`);
const SUB_AGENT = { id: "conv_auth", name: "auth-fix" };

beforeEach(() => {
  announced.length = 0;
  resetBulkRunsForTests();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function page(data: unknown[]): Response {
  return new Response(
    JSON.stringify({ object: "list", data, last_id: null, has_more: false, synced: true }),
    { status: 200 },
  );
}

// A definite refusal; an unclear answer such as a 5xx is settled by a read of the list instead.
const refused = () =>
  new Response(JSON.stringify({ error: { message: "Not allowed" } }), { status: 403 });

/** A server that holds each unlink until the test answers it, in the order they were sent. */
function heldServer() {
  const deletes: { url: string; answer: (response?: Response) => void }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.method !== "DELETE") return Promise.resolve(page([]));
      const url = new URL(String(input), "http://localhost").searchParams.get("url")!;
      return new Promise<Response>((resolve) => {
        deletes.push({ url, answer: (response) => resolve(response ?? page([])) });
      });
    }),
  );
  return deletes;
}

const serverInfo: ServerInfo = { ...FALLBACK_SERVER_INFO, session_pull_requests: true };

function wrapper(client: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesProvider info={serverInfo}>
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </CapabilitiesProvider>
  );
}

function renderProgress(client: QueryClient, conversationId = "conv_1") {
  return renderHook(
    ({ id }) => ({
      progress: useSessionRemovalProgress(id),
      pending: useSessionUnlinkPending(id),
      unlink: useUnlinkSessionPr(id),
    }),
    { initialProps: { id: conversationId }, wrapper: wrapper(client) },
  );
}

/** What the live region says once its pending write lands. */
async function spoken(): Promise<string | null> {
  await act(
    () =>
      new Promise<void>((resolve) => {
        setTimeout(resolve, ANNOUNCE_REFILL_DELAY_MS);
      }),
  );
  return document.querySelector('[data-testid="change-announcer"]')?.textContent ?? null;
}

const keys = (...list: string[]) => new Set(list.map(pullRequestKey));

it("counts a batch as each PR ends, then clears as it settles, saying its start and end once", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const { result } = renderProgress(client);
  expect(result.current.progress).toBeNull();

  const run = startSessionUnlinkRun(client, serverInfo, "conv_1", urls);
  await waitFor(() => expect(deletes).toHaveLength(3));
  expect(result.current.progress).toEqual({
    phase: "removing",
    urls: keys(...urls),
    done: 0,
    total: 3,
    stop: "available",
    runId: runningBulkRuns().at(-1)!.id,
  });
  expect(result.current.pending).toBe(true);
  expect(announced).toEqual(["Removing 3 pull requests…"]);

  deletes[1]!.answer();
  await waitFor(() => expect(result.current.progress?.done).toBe(1));
  deletes[0]!.answer();
  deletes[2]!.answer();
  await run;
  await waitFor(() => expect(result.current.progress).toBeNull());
  expect(result.current.pending).toBe(false);
  // No step is announced, only how it ended.
  expect(announced).toEqual(["Removing 3 pull requests…", "Removed 3 pull requests."]);
  expect(await spoken()).toBe("Removed 3 pull requests.");
});

it("clears a batch with failures as it settles, says what failed, and lets another start", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const { result } = renderProgress(client);
  const run = startSessionUnlinkRun(client, serverInfo, "conv_1", urls);
  await waitFor(() => expect(deletes).toHaveLength(3));
  deletes[0]!.answer();
  deletes[1]!.answer(refused());
  deletes[2]!.answer();
  await run;
  await waitFor(() => expect(result.current.progress).toBeNull());
  expect(announced.at(-1)).toBe(
    "Removed 2 pull requests. 1 pull request couldn’t be removed: Not allowed",
  );

  const again = startSessionUnlinkRun(client, serverInfo, "conv_1", [urls[1]!]);
  await waitFor(() => expect(result.current.progress?.urls).toEqual(keys(urls[1]!)));
  deletes[3]!.answer();
  await again;
  await waitFor(() => expect(result.current.progress).toBeNull());
});

it("stays pending once Stopped until the requests out settle, then clears short of the total", async () => {
  const many = [1, 2, 3, 4, 5].map((number) => `https://github.com/example/app/pull/${number}`);
  const deletes = heldServer();
  const client = new QueryClient();
  const { result } = renderProgress(client);
  const run = startSessionUnlinkRun(client, serverInfo, "conv_1", many);
  await waitFor(() => expect(deletes).toHaveLength(3));

  act(() => stopBulkRun(runningBulkRuns().at(-1)!.id));
  await waitFor(() => expect(result.current.progress?.stop).toBe("requested"));
  // Once asked, it offers no Stop again.
  expect(result.current.progress?.runId).toBeNull();
  expect(result.current.pending).toBe(true);
  deletes[0]!.answer();
  await waitFor(() => expect(result.current.progress?.done).toBe(1));
  expect(result.current.progress?.stop).toBe("requested");

  deletes[1]!.answer();
  deletes[2]!.answer();
  await run;
  await waitFor(() => expect(result.current.progress).toBeNull());
  expect(deletes).toHaveLength(3);
  expect(announced.at(-1)).toBe("Removed 3 pull requests. Stopped before 2 more.");
});

it("reports a single removal with no Stop, and says when the server refused it", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const { result } = renderProgress(client);
  act(() => result.current.unlink.mutate(urls[0]!));
  await waitFor(() =>
    expect(result.current.progress).toEqual({
      phase: "removing",
      urls: keys(urls[0]!),
      done: 0,
      total: 1,
      stop: "none",
      runId: null,
    }),
  );
  expect(announced).toEqual(["Removing the pull request…"]);
  deletes[0]!.answer(refused());
  await waitFor(() => expect(result.current.unlink.isError).toBe(true));
  expect(result.current.progress).toBeNull();
  expect(result.current.pending).toBe(false);
  expect(announced).toEqual([
    "Removing the pull request…",
    "Couldn’t remove the pull request: Not allowed",
  ]);
  expect(await spoken()).toBe("Couldn’t remove the pull request: Not allowed");
});

it("ends a batch that breaks off, so nothing stays busy and another removal can start", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const { result } = renderProgress(client);
  // The run's last step, rereading what it changed, throws.
  vi.spyOn(client, "invalidateQueries").mockImplementationOnce(() => {
    throw new Error("Lost the cache");
  });
  const run = startSessionUnlinkRun(client, serverInfo, "conv_1", [urls[0]!]);
  await waitFor(() => expect(deletes).toHaveLength(1));
  // Completion, the run's end, its cleanup and the unlink settling all land at once.
  await act(async () => {
    deletes[0]!.answer();
    await expect(run).rejects.toThrow("Lost the cache");
  });
  expect(result.current.progress).toBeNull();
  expect(result.current.pending).toBe(false);
  expect(runningBulkRuns()).toEqual([]);
  expect(announced).toEqual([
    "Removing 1 pull request…",
    "Couldn’t finish removing the pull requests. Some may still be linked.",
  ]);
  expect(await spoken()).toBe(
    "Couldn’t finish removing the pull requests. Some may still be linked.",
  );

  const again = startSessionUnlinkRun(client, serverInfo, "conv_1", [urls[1]!]);
  await waitFor(() => expect(deletes).toHaveLength(2));
  deletes[1]!.answer();
  await again;
  await waitFor(() => expect(result.current.progress).toBeNull());
});

it("shows a removal queued behind another session's as waiting, then hands it over", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const first = renderProgress(client, "conv_1");
  const second = renderProgress(client, "conv_2");
  const running = startSessionUnlinkRun(client, serverInfo, "conv_1", [urls[0]!]);
  const queued = startSessionUnlinkRun(client, serverInfo, "conv_2", [urls[1]!, urls[2]!]);
  await waitFor(() => expect(deletes).toHaveLength(1));
  await waitFor(() =>
    expect(second.result.current.progress).toEqual({
      phase: "waiting",
      urls: keys(urls[1]!, urls[2]!),
      done: null,
      total: 2,
      stop: "none",
      runId: null,
    }),
  );
  expect(second.result.current.pending).toBe(true);
  expect(first.result.current.progress?.phase).toBe("removing");
  // A queued run hasn't started, so it hasn't said so yet.
  expect(announced).toEqual(["Removing 1 pull request…"]);

  deletes[0]!.answer();
  await running;
  await waitFor(() =>
    expect(second.result.current.progress).toMatchObject({ phase: "removing", done: 0 }),
  );
  expect(first.result.current.progress).toBeNull();
  expect(announced.at(-1)).toBe("Removing 2 pull requests…");
  deletes[1]!.answer();
  deletes[2]!.answer();
  await queued;
  await waitFor(() => expect(second.result.current.progress).toBeNull());
});

it("reads the same progress after a remount or a session switch, without saying its start again", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const first = renderProgress(client);
  const run = startSessionUnlinkRun(client, serverInfo, "conv_1", urls);
  await waitFor(() => expect(deletes).toHaveLength(3));
  deletes[0]!.answer();
  await waitFor(() => expect(first.result.current.progress?.done).toBe(1));
  first.rerender({ id: "conv_2" });
  expect(first.result.current.progress).toBeNull();
  first.rerender({ id: "conv_1" });
  expect(first.result.current.progress).toMatchObject({ done: 1, total: 3 });
  first.unmount();

  const second = renderProgress(client);
  expect(second.result.current.progress).toMatchObject({ done: 1, total: 3 });
  expect(announced).toEqual(["Removing 3 pull requests…"]);
  deletes[1]!.answer();
  deletes[2]!.answer();
  await run;
  await waitFor(() => expect(second.result.current.progress).toBeNull());
  second.rerender({ id: "conv_1" });
  expect(announced).toEqual(["Removing 3 pull requests…", "Removed 3 pull requests."]);
});

it("reports only a sub-agent's own rows from its parent's run, counting the whole run", async () => {
  const deletes = heldServer();
  const client = new QueryClient();
  const child = renderProgress(client, SUB_AGENT.id);
  const parent = renderProgress(client, "conv_1");
  const row = (url: string, sessionIds: string[]) => ({
    url,
    targets: sessionIds.map((sessionId) => ({ sessionId, url })),
    fromSubAgents: sessionIds.includes(SUB_AGENT.id) ? [SUB_AGENT] : [],
    kept: [],
    ownershipPending: false,
  });
  const run = startTreeUnlinkRun(client, serverInfo, "conv_1", [
    row(urls[0]!, [SUB_AGENT.id]),
    row(urls[1]!, ["conv_1"]),
  ]);
  await waitFor(() => expect(deletes).toHaveLength(2));
  await waitFor(() =>
    expect(child.result.current.progress).toMatchObject({ urls: keys(urls[0]!), total: 2 }),
  );
  expect(parent.result.current.progress?.urls).toEqual(keys(urls[0]!, urls[1]!));
  client.setQueryData(sessionPullRequestsKey("conv_1"), { prs: [], synced: true });
  for (const each of deletes) each.answer();
  await run;
  await waitFor(() => expect(child.result.current.progress).toBeNull());
});
