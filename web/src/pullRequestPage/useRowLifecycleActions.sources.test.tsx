// Two lists on one page share a recovery scope: the table, and the source beside it that reads
// Recently reviewed's PRs. A Convert to draft's Undo runs through whichever lists the PR when it's
// clicked, with that list's read of it, however the two mounted.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { toast, toastError } = vi.hoisted(() => ({ toast: vi.fn(), toastError: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: toastError, dismiss: vi.fn() }),
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  LIFECYCLE_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests } from "./pullRequestStateChain";
import { type LifecycleRow, useRowLifecycleActions } from "./useRowLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const AUTOMATIC = { reader: null };
const SCOPE = "page-recovery";
const changedAs = (request: Record<string, unknown>) => ({
  ok: true,
  data: {
    object: "session.github.pr_draft",
    pr_url: PR,
    is_draft: request.operation === "pull_request_convert_to_draft",
  },
  actor: { login: "alice" },
});
/** The same PR as another list may write it. */
const SPELLED = "https://GitHub.com/ACME/App/pull/42/";
const rowOf = (pr: LifecycleRow["pr"], url = PR): LifecycleRow => ({
  url,
  number: 42,
  paletteName: "#42 Ship it",
  pr,
});
const OPEN = { state: "OPEN", is_draft: false, head_sha: "a".repeat(40) };

function renderList(listed: Map<string, LifecycleRow>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={AUTOMATIC}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  return renderHook(() => useRowLifecycleActions("pull-requests-page", true, listed, SCOPE), {
    wrapper,
  });
}

beforeEach(() => {
  toast.mockReset();
  toastError.mockReset();
  resetStateChainsForTests();
  resetLifecycleActionsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  cleanup();
  setLifecycleFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  resetLifecycleActionsForTests();
  removeNamedWriteShell();
});

describe.each([
  ["the table mounted first", "table", PR],
  ["the source beside it mounted first", "beside", PR],
  ["the table mounted first, beside writing the link another way", "table", SPELLED],
  ["the source beside it mounted first, writing the link another way", "beside", SPELLED],
] as const)("with %s", (_label, first, besideUrl) => {
  async function convertedThenHandedOver(
    besideRead: LifecycleRow["pr"],
    { refusedOnce = false }: { refusedOnce?: boolean } = {},
  ) {
    let converts = 0;
    const shell = installNamedWriteShell(
      (request) =>
        refusedOnce && request.operation === "pull_request_convert_to_draft" && ++converts === 1
          ? { ok: false, error: { code: "rejected", message: "Refused once." } }
          : changedAs(request),
      { extraWrites: LIFECYCLE_WRITE_OPERATIONS },
    );
    await shell.ready(PR);
    const tableRows = new Map([[PR, rowOf(OPEN)]]);
    const besideRows = new Map<string, LifecycleRow>();
    const views =
      first === "table"
        ? { table: renderList(tableRows), beside: renderList(besideRows) }
        : { beside: renderList(besideRows), table: renderList(tableRows) };
    const convert = views.table.result.current
      .actionsFor(rowOf(OPEN))
      .find((action) => action.id === "lifecycle:convert_to_draft");
    act(() => convert!.run());
    await waitFor(() =>
      refusedOnce
        ? expect(toastError).toHaveBeenCalled()
        : expect(String(toast.mock.calls.at(-1)?.[0])).toMatch(/^Converted/),
    );
    // Undo once converted; Try Again once refused.
    const undo = (
      (refusedOnce ? toastError : toast).mock.calls.at(-1)![1] as {
        action: { onClick: () => void };
      }
    ).action;
    // The table no longer lists the PR; the source beside it reads it.
    tableRows.clear();
    besideRows.set(besideUrl, rowOf(besideRead, besideUrl));
    views.table.rerender();
    views.beside.rerender();
    act(() => undo.onClick());
    await act(async () => {});
    return shell;
  }

  it("marks it ready again where the list that has it now reads it a draft", async () => {
    const shell = await convertedThenHandedOver({ ...OPEN, is_draft: true });
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
    expect(shell.codeHostWrite.mock.calls[1]![0]).toMatchObject({
      operation: "pull_request_ready_for_review",
    });
  });

  it("sends nothing where the list that has it now reads it merged", async () => {
    const shell = await convertedThenHandedOver({ ...OPEN, state: "MERGED", is_draft: false });
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  });

  it("tries a refused Convert again where the list that has it now still finds it ready", async () => {
    const shell = await convertedThenHandedOver(OPEN, { refusedOnce: true });
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
    expect(shell.codeHostWrite.mock.calls[1]![0]).toMatchObject({
      operation: "pull_request_convert_to_draft",
    });
  });

  it("tries nothing again where the list that has it now reads it merged", async () => {
    const shell = await convertedThenHandedOver(
      { ...OPEN, state: "MERGED" },
      { refusedOnce: true },
    );
    expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  });
});
