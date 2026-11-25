// Changes across a push: the previous push's files and patch stay, from the same shared record,
// until both of the latest push's land, and never once either half or the record's owner moved.

import { QueryClient, QueryClientProvider, type UseQueryResult } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const identity = vi.hoisted(() => ({ userId: "user-1" as string | null }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.userId,
}));

import {
  pullRequestChangedFilesQueryOptions,
  pullRequestDiffQueryOptions,
  useDesktopPullRequestReads,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import {
  acceptMacRead,
  advanceMacReadScope,
  beginMacRead,
  macReadScopeKey,
  refuseMacReadScope,
  resetMacReadScopesForTests,
} from "@/lib/macReadScope";

import { useShownChangesPair } from "./useShownChangesPair";

const PR = "https://github.com/acme/app/pull/7";
const FIRST = "base:head-1";
const SECOND = "base:head-2";
const THIRD = "base:head-3";
const scope = () => macReadScopeKey("github", "github.com", "user-1");

interface Files {
  files: string;
}
interface Patch {
  patch: string;
}

/** A read's result as the panel's hooks give it: answered with `data`, reading, or failed. */
function result<Data>(data: Data | undefined, error: Error | null = null): UseQueryResult<Data> {
  return {
    data,
    error,
    isLoading: data === undefined && !error,
    isPending: data === undefined && !error,
    isSuccess: data !== undefined,
    isError: !!error,
    status: error ? "error" : data === undefined ? "pending" : "success",
    refetch: vi.fn(),
  } as unknown as UseQueryResult<Data>;
}

function installShell(proves: boolean) {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(() => new Promise(() => {})),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS],
          accounts: true,
          ...(proves ? { answerProvenance: 1 } : {}),
        },
      ],
    }),
  };
}

interface Live {
  revision: string;
  files: UseQueryResult<Files>;
  patch: UseQueryResult<Patch>;
  /** The view marks a kept pair as outdated; on unless a test says otherwise. */
  keepPrevious?: boolean;
}

/** Changes for `PR`, with its live reads given by the test, on one QueryClient. */
function renderChanges(client: QueryClient, initial: Live) {
  const view = renderHook(
    ({ revision, files, patch, keepPrevious = true }: Live) => {
      const reads = useDesktopPullRequestReads();
      return {
        reads,
        shown: useShownChangesPair("conv_1", PR, revision, files, patch, { keepPrevious }),
      };
    },
    {
      initialProps: initial,
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    },
  );
  // Plain keys: the test caches its own stand-ins for each half's answer under them.
  const keys = (revision: string) => ({
    files: [
      ...pullRequestChangedFilesQueryOptions("conv_1", PR, revision, view.result.current.reads)
        .queryKey,
    ] as unknown[],
    patch: [
      ...pullRequestDiffQueryOptions("conv_1", PR, revision, view.result.current.reads).queryKey,
    ] as unknown[],
  });
  return { view, keys };
}

/** Both halves at `revision` read and cached, as the live reads hold them. */
function answered(client: QueryClient, keys: { files: unknown[]; patch: unknown[] }, at: string) {
  const files = { files: `files at ${at}` };
  const patch = { patch: `patch at ${at}` };
  client.setQueryData(keys.files, files);
  client.setQueryData(keys.patch, patch);
  return { revision: at, files: result(files), patch: result(patch) };
}

const reading = (revision: string): Live => ({
  revision,
  files: result<Files>(undefined),
  patch: result<Patch>(undefined),
});

const shownOf = (view: ReturnType<typeof renderChanges>["view"]) => {
  const { changes, prDiff, revision, outdated } = view.result.current.shown;
  return { files: changes.data, patch: prDiff.data, revision, outdated };
};

let client: QueryClient;

/** The first push read whole, then a push to `SECOND`, its reads out. */
async function pushedWhileShowing(keepPrevious = true) {
  const rendered = renderChanges(client, { ...reading(FIRST), keepPrevious });
  const first = answered(client, rendered.keys(FIRST), FIRST);
  rendered.view.rerender({ ...first, keepPrevious });
  rendered.view.rerender({ ...reading(SECOND), keepPrevious });
  return rendered;
}

beforeEach(async () => {
  identity.userId = "user-1";
  resetMacReadScopesForTests();
  installShell(true);
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  acceptMacRead(beginMacRead(scope()), "alice");
  client = new QueryClient();
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
});

describe("useShownChangesPair", () => {
  it("keeps the previous push's pair, marked outdated, while the latest push's is read", async () => {
    const { view } = await pushedWhileShowing();
    expect(shownOf(view)).toEqual({
      files: { files: `files at ${FIRST}` },
      patch: { patch: `patch at ${FIRST}` },
      revision: FIRST,
      outdated: true,
    });
  });

  it.each(["files", "patch"] as const)(
    "swaps in the latest push's pair only once both halves land, the %s first",
    async (first) => {
      const { view, keys } = await pushedWhileShowing();
      const latest = answered(client, keys(SECOND), SECOND);
      view.rerender({
        ...reading(SECOND),
        ...(first === "files" ? { files: latest.files } : { patch: latest.patch }),
      });
      expect(shownOf(view)).toMatchObject({ revision: FIRST, outdated: true });
      view.rerender(latest);
      expect(shownOf(view)).toEqual({
        files: { files: `files at ${SECOND}` },
        patch: { patch: `patch at ${SECOND}` },
        revision: SECOND,
        outdated: false,
      });
    },
  );

  it("keeps the pair it showed through a second push while the first is still read", async () => {
    const { view } = await pushedWhileShowing();
    view.rerender(reading(THIRD));
    expect(shownOf(view)).toMatchObject({ revision: FIRST, outdated: true });
  });

  it.each(["files", "patch"] as const)(
    "shows the latest push's failure, not the previous pair, when its %s read fails",
    async (failed) => {
      const { view } = await pushedWhileShowing();
      const failure = new Error("GitHub didn't answer in time.");
      view.rerender({
        ...reading(SECOND),
        ...(failed === "files"
          ? { files: result<Files>(undefined, failure) }
          : { patch: result<Patch>(undefined, failure) }),
      });
      expect(shownOf(view)).toEqual({
        files: undefined,
        patch: undefined,
        revision: SECOND,
        outdated: false,
      });
    },
  );

  it.each([
    ["this Mac reads as another account", () => acceptMacRead(beginMacRead(scope()), "bob")],
    ["the account is refused", () => refuseMacReadScope(scope())],
    [
      "the same login signs out and back in",
      () => {
        advanceMacReadScope(scope(), "left");
        acceptMacRead(beginMacRead(scope()), "alice");
      },
    ],
  ])("shows nothing of the previous pair once %s", async (_move, move) => {
    const { view } = await pushedWhileShowing();
    act(() => move());
    await waitFor(() => expect(shownOf(view).outdated).toBe(false));
    expect(shownOf(view).files).toBeUndefined();
  });

  it.each(["files", "patch"] as const)(
    "shows nothing kept once the previous %s is removed, and again once it's back",
    async (half) => {
      const { view, keys } = await pushedWhileShowing();
      const previous = keys(FIRST);
      act(() => client.removeQueries({ queryKey: previous[half], exact: true }));
      expect(shownOf(view)).toMatchObject({ files: undefined, outdated: false });
      act(() => {
        client.setQueryData(
          previous[half],
          half === "files" ? { files: "back" } : { patch: "back" },
        );
      });
      expect(shownOf(view)).toMatchObject({ revision: FIRST, outdated: true });
    },
  );

  it("keeps nothing for a view that doesn't mark the pair as outdated", async () => {
    const { view } = await pushedWhileShowing(false);
    expect(shownOf(view)).toEqual({
      files: undefined,
      patch: undefined,
      revision: SECOND,
      outdated: false,
    });
  });

  it("keeps nothing for a session's own reads, which no proven reader owns", async () => {
    resetDesktopReadPathForTests();
    installShell(false);
    await desktopReadTarget(PR);
    const { view } = await pushedWhileShowing();
    expect(shownOf(view)).toEqual({
      files: undefined,
      patch: undefined,
      revision: SECOND,
      outdated: false,
    });
  });
});
