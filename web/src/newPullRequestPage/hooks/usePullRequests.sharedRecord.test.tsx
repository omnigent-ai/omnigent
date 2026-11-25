// A PR read on this Mac is one record per reader, whichever view shows it: two sessions and the
// Pull Requests page share one entry and one read, and a record only ever holds answers read as
// its own reader in its own epoch, through real hooks, a real QueryClient and a fake shell.

import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: unknown) =>
    typeof selector === "function"
      ? (selector as (state: { conversationId: null; sessionStatus: "idle" }) => unknown)({
          conversationId: null,
          sessionStatus: "idle",
        })
      : undefined,
}));

import {
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { macReadScopeKey, resetMacReadScopesForTests } from "@/lib/macReadScope";

import { usePullRequestChangedFiles, usePullRequestInfo } from "./usePullRequests";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/42";

interface Shell {
  login: string;
  /** The sign-in the login reads under; a switch of account is a new one. */
  generation: number;
  /** Answer this code instead of reading, until cleared, as a signed-out account does. */
  refusing: string | null;
  /** Hold `pull_request_files` reads until released. */
  hold: Promise<void> | null;
}

/** A shell reading github.com as `shell.login`, proving each answer's viewer, or not. */
function installShell({ proves = true }: { proves?: boolean } = {}) {
  const shell: Shell = { login: "alice", generation: 1, refusing: null, hold: null };
  const codeHostQuery = vi.fn(async (request: Record<string, unknown>) => {
    if (request.operation === "account_list") {
      return { ok: true, data: [{ login: shell.login, active: true, state: "success" }] };
    }
    // Who reads is decided as the request goes out; a switch while it waits doesn't change it.
    const { login, generation } = shell;
    if (request.operation === "pull_request_overview") {
      return {
        ok: true,
        data: {
          repository: "acme/app",
          pr: { number: 42, title: `Read as ${shell.login}`, state: "OPEN", url: PR },
        },
        identity: { host: "github.com", login: shell.login, generation: shell.generation },
        ...(proves
          ? {
              provenance: {
                version: 1,
                provider: "github",
                host: "github.com",
                viewer: shell.login,
              },
            }
          : {}),
      };
    }
    if (request.operation === "pull_request_files" && shell.hold) await shell.hold;
    if (shell.refusing) {
      return { ok: false, error: { code: shell.refusing, message: "Sign in again." } };
    }
    return {
      ok: true,
      data: {
        data: [{ path: `${login}.ts`, status: "modified", additions: 1, deletions: 0 }],
        has_more: false,
      },
      identity: { host: "github.com", login, generation },
      ...(proves
        ? { provenance: { version: 1, provider: "github", host: "github.com", viewer: login } }
        : {}),
    };
  });
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
      onNamedChanged: () => () => {},
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
  const reads = () =>
    codeHostQuery.mock.calls.filter(([request]) => request.operation === "pull_request_files");
  /** This Mac's account switches to `login`, as signing in to it does. */
  const switchTo = (login: string) => {
    shell.login = login;
    shell.generation += 1;
  };
  return { shell, reads, switchTo, codeHostQuery };
}

async function ready() {
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
}

const shown = new Map<string, string[] | undefined>();

function Files({ session }: { session: string }) {
  shown.set(
    session,
    usePullRequestChangedFiles(session, true, PR, "base:head").data?.data.map((file) => file.path),
  );
  return null;
}

function renderViews(client: QueryClient, sessions: readonly string[]) {
  return render(
    <QueryClientProvider client={client}>
      {sessions.map((session) => (
        <Files key={session} session={session} />
      ))}
    </QueryClientProvider>,
  );
}

const filesEntries = (client: QueryClient) =>
  client
    .getQueryCache()
    .findAll({ queryKey: ["github-changed-files"] })
    .map((query) => ({ key: query.queryKey, data: query.state.data }));

/** The reader each cached entry's key names and the files it holds: never another's. */
const readersAndFiles = (client: QueryClient) =>
  filesEntries(client).map(({ key, data }) => ({
    reader: key[key.indexOf("reader") + 1],
    files: (data as { data: { path: string }[] } | undefined)?.data.map((file) => file.path),
  }));

afterEach(() => {
  cleanup();
  shown.clear();
  githubMacReadsPreference.write("automatic");
  resetMacNamedReadsForTests();
  resetDesktopReadPathForTests();
  resetMacReadScopesForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

const titles = new Map<string, string | undefined>();

/** A view of the PR's overview, as the page's detail or a session's panel reads it. */
function Overview({ session }: { session: string }) {
  titles.set(session, usePullRequestInfo(session, { prUrl: PR }).data?.pr?.title ?? undefined);
  return null;
}

describe("a PR's shared overview on this Mac", () => {
  it("is read once for the Pull Requests page's detail and a session's panel", async () => {
    const { codeHostQuery } = installShell();
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Overview session="pull-requests-page" />
        <Overview session="conv_a" />
      </QueryClientProvider>,
    );
    await waitFor(() => {
      expect(titles.get("pull-requests-page")).toBe("Read as alice");
      expect(titles.get("conv_a")).toBe("Read as alice");
    });
    const overviewReads = codeHostQuery.mock.calls.filter(
      ([request]) => request.operation === "pull_request_overview",
    );
    expect(overviewReads).toHaveLength(1);
    titles.clear();
  });
});

describe("a PR's shared record on this Mac", () => {
  it("is one entry and one read for every view of the PR", async () => {
    const { reads } = installShell();
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderViews(client, ["conv_a", "conv_b", "pull-requests-page"]);

    await waitFor(() => {
      for (const session of ["conv_a", "conv_b", "pull-requests-page"]) {
        expect(shown.get(session)).toEqual(["alice.ts"]);
      }
    });
    expect(reads()).toHaveLength(1);
    await waitFor(() => expect(filesEntries(client)).toHaveLength(1));
    expect(readersAndFiles(client)).toEqual([{ reader: "alice", files: ["alice.ts"] }]);
  });

  it("never keeps an answer read as Bob under Alice's record, though Alice's read asked", async () => {
    const { reads, switchTo } = installShell();
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderViews(client, ["conv_a"]);
    await waitFor(() => expect(shown.get("conv_a")).toEqual(["alice.ts"]));
    const aliceKey = filesEntries(client)[0]!.key;

    // Something still shows Alice's record as the view moves on, so its read lands in it.
    const stillShown = new QueryObserver(client, { queryKey: aliceKey, enabled: false });
    const stopShowing = stillShown.subscribe(() => {});
    // Alice's record reads again, and the account on this Mac is Bob's by the time it reads.
    switchTo("bob");
    await act(() => client.invalidateQueries({ queryKey: aliceKey, exact: true }));

    await waitFor(() => expect(shown.get("conv_a")).toEqual(["bob.ts"]));
    expect(reads().length).toBeGreaterThanOrEqual(2);
    for (const entry of readersAndFiles(client)) {
      if (entry.files) expect(entry.files).toEqual([`${String(entry.reader)}.ts`]);
    }
    await waitFor(() => expect(client.getQueryState(aliceKey)?.fetchStatus).toBe("idle"));
    expect(client.getQueryData(aliceKey)).toBeUndefined();
    stopShowing();
  });

  it("shows nothing of Alice after a switch to Bob and back, and reads Alice anew", async () => {
    const { switchTo } = installShell();
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderViews(client, ["conv_a"]);
    await waitFor(() => expect(shown.get("conv_a")).toEqual(["alice.ts"]));
    const firstAlice = filesEntries(client)[0]!.key;

    // One switch at a time: each is read and shown before the next.
    /* oxlint-disable no-await-in-loop */
    for (const login of ["bob", "alice"]) {
      switchTo(login);
      await act(() => client.invalidateQueries({ queryKey: ["github-changed-files"] }));
      await waitFor(() => expect(shown.get("conv_a")).toEqual([`${login}.ts`]));
    }
    /* oxlint-enable no-await-in-loop */
    const current = filesEntries(client).filter(({ data }) => data !== undefined);
    expect(current).toHaveLength(1);
    expect(current[0]!.key).not.toEqual(firstAlice);
    expect(client.getQueryData(firstAlice)).toBeUndefined();
  });

  it("drops what Alice showed when her account is refused, and keeps nothing for her return", async () => {
    const { shell, reads } = installShell();
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderViews(client, ["conv_a"]);
    await waitFor(() => expect(shown.get("conv_a")).toEqual(["alice.ts"]));
    const before = filesEntries(client)[0]!.key;

    shell.refusing = "account_invalid";
    await act(() => client.invalidateQueries({ queryKey: ["github-changed-files"] }));
    await waitFor(() => expect(shown.get("conv_a")).toBeUndefined());
    expect(client.getQueryData(before)).toBeUndefined();
    // The refused record is read once more under its new epoch, refused again, and left at that.
    const refusedReads = reads().length;
    await new Promise((resolve) => {
      setTimeout(resolve, 100);
    });
    expect(reads().length).toBe(refusedReads);
    expect(shown.get("conv_a")).toBeUndefined();

    // Alice signs in again: her answer goes into a record of its own, not the one refused.
    shell.refusing = null;
    await act(() => client.invalidateQueries({ queryKey: ["github-changed-files"] }));
    await waitFor(() => expect(shown.get("conv_a")).toEqual(["alice.ts"]));
    const after = filesEntries(client).find(({ data }) => data !== undefined)!.key;
    expect(after).not.toEqual(before);
  });

  it("stays each session's own where the shell can't prove who read it", async () => {
    const { reads } = installShell({ proves: false });
    await ready();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    renderViews(client, ["conv_a", "conv_b"]);
    await waitFor(() => {
      expect(shown.get("conv_a")).toEqual(["alice.ts"]);
      expect(shown.get("conv_b")).toEqual(["alice.ts"]);
    });
    const keys = filesEntries(client).map(({ key }) => key);
    expect(keys).toHaveLength(2);
    expect(keys.map((key) => key[1]).sort()).toEqual(["conv_a", "conv_b"]);
    expect(keys.every((key) => !key.includes("reader"))).toBe(true);
    expect(reads()).toHaveLength(2);
    // The session keys still end with the scope the purge finds them by.
    const scope = macReadScopeKey("github", "github.com");
    for (const key of keys) expect(key.slice(-scope.length)).toEqual([...scope]);
  });
});
