// A row's Close toast offers Undo, bound to everything the close was made under: the workspace
// user and sign-in as well as the Mac account. Signing in as another workspace user, or signing
// out and in again as the same one, leaves the old toast's Undo sending nothing, even after a
// fresh read as the same Mac account. A Close that succeeds on Try Again offers Undo the same way.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { identity, toast, toastError } = vi.hoisted(() => ({
  identity: { user: "user-1" as string | null, listeners: new Set<() => void>() },
  toast: vi.fn(),
  toastError: vi.fn(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { error: toastError, success: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.user,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { resetStateChainsForTests } from "@/pullRequestPage/pullRequestStateChain";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import type { PullRequestListEntry } from "./PullRequestListRows";
import { useRowStateChanges } from "./useRowStateChanges";

const PR = "https://github.com/acme/app/pull/42";
const WRITES = ["pull_request_close", "pull_request_reopen"];
const AUTOMATIC = { reader: null };
const answer = (request: Record<string, unknown>) => ({
  ok: true,
  data: {
    object: "session.github.pr_state",
    pr_url: PR,
    state: request.operation === "pull_request_close" ? "CLOSED" : "OPEN",
  },
  actor: { login: "alice" },
});

interface ToastOptions {
  action: { label: string; onClick: () => void };
  duration?: number;
  closeButton?: boolean;
}

function renderRow() {
  const live = { state: "OPEN" };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={AUTOMATIC}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  const hook = renderHook(
    () => {
      const entry = {
        association: { url: PR, repository: "acme/app", number: 42, provider: "github" },
        row: { url: PR, repository: "acme/app", number: 42, title: "Build" },
        pr: { url: PR, state: live.state, is_draft: false },
      } as unknown as PullRequestListEntry;
      const changes = useRowStateChanges({
        conversationId: "pull-requests-page",
        entriesByUrl: new Map([[PR, entry]]),
        onOpen: () => {},
      });
      return { ...changes, action: changes.actionFor(entry, "#42") };
    },
    { wrapper },
  );
  return { hook, live };
}

/** The workspace user signs in as `user`; null signs out. */
function signInAs(user: string | null) {
  act(() => {
    identity.user = user;
    for (const listener of identity.listeners) listener();
  });
}

/** Close the row, and the Undo its toast then offers. */
async function closedWithUndo(shell: ReturnType<typeof installNamedWriteShell>) {
  const view = renderRow();
  act(() => view.hook.result.current.action!.run());
  await waitFor(() =>
    expect(toast).toHaveBeenCalledWith("Closed #42 as @alice", expect.anything()),
  );
  const undo = (toast.mock.calls.at(-1)![1] as ToastOptions).action;
  expect(undo.label).toBe("Undo");
  view.live.state = "CLOSED";
  view.hook.rerender();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  return { ...view, undo: undo.onClick };
}

beforeEach(() => {
  identity.user = "user-1";
  toast.mockReset();
  toastError.mockReset();
  resetStateChainsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  cleanup();
  setLifecycleFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

it("reopens on Undo as the account and sign-in the close was made under", async () => {
  const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
  await shell.ready(PR);
  const { undo } = await closedWithUndo(shell);
  act(() => undo());
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  expect(shell.codeHostWrite.mock.calls[1]![0]).toMatchObject({ operation: "pull_request_reopen" });
});

it.each([
  ["another workspace user signed in", () => signInAs("user-2")],
  [
    "the same workspace user signed out and in again",
    () => {
      signInAs(null);
      signInAs("user-1");
    },
  ],
])("sends no Undo once %s, even after a fresh read as the same Mac account", async (_l, change) => {
  const shell = installNamedWriteShell(answer, { extraWrites: WRITES });
  await shell.ready(PR);
  const { hook, undo } = await closedWithUndo(shell);
  change();
  await act(async () => shell.readAsHost(PR));
  hook.rerender();
  act(() => undo());
  await act(async () => {});
  expect(toast.mock.calls.at(-1)![0]).toBe("#42 wasn’t changed back: the account changed since.");
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
});

it("names the account and offers Undo once a refused Close succeeds on Try Again", async () => {
  let attempts = 0;
  const shell = installNamedWriteShell(
    (request) =>
      ++attempts === 1
        ? { ok: false, error: { code: "rejected", message: "Refused once." } }
        : answer(request),
    { extraWrites: WRITES },
  );
  await shell.ready(PR);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  await waitFor(() => expect(toastError).toHaveBeenCalled());
  const refused = toastError.mock.calls.at(-1)![1] as ToastOptions & { id: string };
  // The refusal stays until it's tried again or closed.
  expect(refused).toMatchObject({ duration: Number.POSITIVE_INFINITY, closeButton: true });
  act(() => refused.action.onClick());
  await waitFor(() =>
    expect(toast).toHaveBeenCalledWith("Closed #42 as @alice", expect.anything()),
  );
  // The success replaces the refusal's toast.
  expect((toast.mock.calls.at(-1)![1] as { id: string }).id).toBe(refused.id);
  expect((toast.mock.calls.at(-1)![1] as ToastOptions).action.label).toBe("Undo");
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(2);
});

it("tries a refused Undo again even once the page no longer lists the PR", async () => {
  let reopens = 0;
  const shell = installNamedWriteShell(
    (request) =>
      request.operation === "pull_request_reopen" && ++reopens === 1
        ? { ok: false, error: { code: "rejected", message: "Refused once." } }
        : answer(request),
    { extraWrites: WRITES },
  );
  await shell.ready(PR);
  const { hook, live, undo } = await closedWithUndo(shell);
  // The page took the closed row out of its list.
  live.state = "";
  hook.rerender();
  act(() => undo());
  await waitFor(() => expect(toastError).toHaveBeenCalled());
  act(() => (toastError.mock.calls.at(-1)![1] as ToastOptions).action.onClick());
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(3));
  expect(shell.codeHostWrite.mock.calls.map(([request]) => request.operation)).toEqual([
    "pull_request_close",
    "pull_request_reopen",
    "pull_request_reopen",
  ]);
});
