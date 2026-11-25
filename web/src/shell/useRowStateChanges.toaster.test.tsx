// A row's Close toasts through the real Toaster: Try Again and Undo keep their toast up, saying
// what's under way, until the outcome replaces it, so a refusal or a fresh Undo can't vanish on
// Sonner's own close after the click. A second click while one runs keeps the row busy, and
// an action that ends sending nothing, once the workspace user changed, leaves no toast held.
// What runs is read from the stores, so it outlives the row and shows wherever it started.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { toast, Toaster } from "sonner";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const { identity } = vi.hoisted(() => ({
  identity: { user: "user-1", listeners: new Set<() => void>() },
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => identity.user,
  subscribeIdentity: (listener: () => void) => {
    identity.listeners.add(listener);
    return () => identity.listeners.delete(listener);
  },
}));

/** Another workspace user signs in. */
function signInAs(user: string) {
  act(() => {
    identity.user = user;
    for (const listener of identity.listeners) listener();
  });
}

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { pullRequestChangeInFlight } from "@/pullRequestPage/pullRequestChangeInFlight";
import { resetStateChainsForTests, stateChainKey } from "@/pullRequestPage/pullRequestStateChain";
import { usePullRequestStateAction } from "@/pullRequestPage/usePullRequestStateAction";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import type { PullRequestListEntry } from "./PullRequestListRows";
import { useRowStateChanges } from "./useRowStateChanges";

const PR = "https://github.com/acme/app/pull/42";
const WRITES = ["pull_request_close", "pull_request_reopen"];
const AUTOMATIC = { reader: null };
const AS_BOB = { reader: BOB };
/** The page's, which each table it mounts recovers its toasts through. */
const RECOVERY = "pull-requests-page-recovery";
const changed = (state: "OPEN" | "CLOSED") => ({
  ok: true,
  data: { object: "session.github.pr_state", pr_url: PR, state },
  actor: { login: "alice" },
});
const refused = { ok: false, error: { code: "rejected", message: "Refused." } };
const asked = (request: Record<string, unknown>) =>
  changed(request.operation === "pull_request_close" ? "CLOSED" : "OPEN");

/** The change running for the PR, as every surface reads it from the stores. */
const inFlight = () =>
  pullRequestChangeInFlight(stateChainKey(identity.user, PR))?.operation ?? null;

function renderRow(reader: typeof BOB | null = null) {
  // The PR as last read; null before any read.
  const live: { state: string | null } = { state: "OPEN" };
  const readAs = reader ? AS_BOB : AUTOMATIC;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={readAs}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  const hook = renderHook(
    () => {
      const entry = {
        association: { url: PR, repository: "acme/app", number: 42, provider: "github" },
        row: { url: PR, repository: "acme/app", number: 42, title: "Build" },
        pr: live.state ? { url: PR, state: live.state, is_draft: false } : undefined,
      } as unknown as PullRequestListEntry;
      const changes = useRowStateChanges({
        conversationId: "pull-requests-page",
        recoveryScope: RECOVERY,
        entriesByUrl: new Map([[PR, entry]]),
        onOpen: () => {},
      });
      return { ...changes, action: changes.actionFor(entry, "#42 Build") };
    },
    { wrapper },
  );
  return { hook, live, wrapper };
}

/** How long the toast saying `title` stays, as the Toaster keeps it; undefined: its default. */
const durationOf = (title: string) => {
  for (const shown of toast.getToasts()) {
    if ("title" in shown && shown.title === title) return shown.duration;
  }
  throw new Error(`No toast says ${title}`);
};

/** Past Sonner's close animation, which would take a toast it closed out of the page. */
const settle = () =>
  act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 400);
    });
  });

/** A shell that answers each write in turn from `answers`, a promise held until it settles. */
async function shellAnswering(answers: unknown[]) {
  const shell = installNamedWriteShell(() => answers.shift(), { extraWrites: WRITES });
  await shell.ready(PR);
  render(<Toaster />);
  return shell;
}

function held() {
  let resolve!: (answer: unknown) => void;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  identity.user = "user-1";
  toast.dismiss();
  resetStateChainsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  toast.dismiss();
  cleanup();
  setLifecycleFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

it("keeps the refusal's toast, saying it's closing, while Try Again is unanswered", async () => {
  const retry = held();
  const shell = await shellAnswering([refused, retry.promise]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  fireEvent.click(await screen.findByRole("button", { name: "Try Again" }));
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  await settle();
  expect(screen.getByText("Closing #42…")).toBeTruthy();
  // Nothing to click again while it runs.
  expect(screen.queryByRole("button", { name: "Try Again" })).toBeNull();
  await act(async () => retry.resolve(changed("CLOSED")));
  await settle();
  expect(screen.getByText("Closed #42 as @alice")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
  // Once made, it goes on the Toaster's own timing rather than staying like the refusal.
  expect(durationOf("Closed #42 as @alice")).toBeUndefined();
});

it("shows a second refusal from Try Again, however fast it comes", async () => {
  const shell = await shellAnswering([refused, refused]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  fireEvent.click(await screen.findByRole("button", { name: "Try Again" }));
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  await settle();
  expect(screen.getByRole("button", { name: "Try Again" })).toBeTruthy();
  expect(screen.getByText(/Refused\./)).toBeTruthy();
});

it("keeps the toast a fast Undo's reopen replaces it with, offering Undo again", async () => {
  const shell = installNamedWriteShell(asked, { extraWrites: WRITES });
  await shell.ready(PR);
  render(<Toaster />);
  const { hook, live } = renderRow();
  act(() => hook.result.current.action!.run());
  await screen.findByText("Closed #42 as @alice");
  live.state = "CLOSED";
  hook.rerender();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  await settle();
  expect(screen.getByText("Reopened #42 as @alice")).toBeTruthy();
  expect(screen.getByRole("button", { name: "Undo" })).toBeTruthy();
  expect(durationOf("Reopened #42 as @alice")).toBeUndefined();
});

it("keeps the row busy through a double click on Try Again, sending it once", async () => {
  const retry = held();
  const shell = await shellAnswering([refused, retry.promise]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  const button = await screen.findByRole("button", { name: "Try Again" });
  act(() => {
    fireEvent.click(button);
    fireEvent.click(button);
  });
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(2));
  await settle();
  expect(inFlight()).toBe("pull_request_close");
  expect(screen.getByText("Closing #42…")).toBeTruthy();
  await act(async () => retry.resolve(changed("CLOSED")));
  await waitFor(() => expect(inFlight()).toBeNull());
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(2);
});

it("keeps a row's close running once the row goes, and still says what it came to", async () => {
  const close = held();
  const shell = await shellAnswering([close.promise]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(1));
  hook.unmount();
  expect(inFlight()).toBe("pull_request_close");
  await act(async () => close.resolve(changed("CLOSED")));
  await waitFor(() => expect(inFlight()).toBeNull());
  expect(await screen.findByText("Closed #42 as @alice")).toBeTruthy();
});

it("holds a row's Close while the PR page's own close runs, and frees it once made", async () => {
  const close = held();
  const shell = await shellAnswering([close.promise]);
  const { hook, wrapper } = renderRow();
  const page = renderHook(
    () =>
      usePullRequestStateAction({
        conversationId: "pull-requests-page",
        prUrl: PR,
        prState: "OPEN",
        post: async () => true,
      }),
    { wrapper },
  );
  act(() => page.result.current.run(false));
  await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(1));
  expect(inFlight()).toBe("pull_request_close");
  hook.rerender();
  expect(hook.result.current.action!.availability).toMatchObject({ available: false });
  act(() => hook.result.current.action!.run());
  await act(async () => close.resolve(changed("CLOSED")));
  await waitFor(() => expect(inFlight()).toBeNull());
  // The row's click while it ran sent nothing of its own.
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
});

it("sends no Undo once the table that replaced it reads as another login", async () => {
  const shell = await shellAnswering([changed("CLOSED")]);
  const first = renderRow();
  act(() => first.hook.result.current.action!.run());
  await screen.findByText("Closed #42 as @alice");
  first.hook.unmount();
  renderRow(BOB).live.state = "CLOSED";
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(screen.getByText("#42 wasn’t changed back: the account changed since.")).toBeTruthy();
});

it("sends no Try Again once the table that replaced it reads as another login", async () => {
  const shell = await shellAnswering([refused]);
  const first = renderRow();
  act(() => first.hook.result.current.action!.run());
  const tryAgain = await screen.findByRole("button", { name: "Try Again" });
  first.hook.unmount();
  renderRow(BOB);
  fireEvent.click(tryAgain);
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
});

it("sends no Try Again for another workspace user's refusal of the same PR", async () => {
  const shell = await shellAnswering([refused, refused, changed("CLOSED")]);
  const first = renderRow();
  act(() => first.hook.result.current.action!.run());
  const earlier = await screen.findByRole("button", { name: "Try Again" });
  first.hook.unmount();
  signInAs("user-2");
  await act(async () => shell.readAsHost(PR));
  const second = renderRow();
  act(() => second.hook.result.current.action!.run());
  await waitFor(() => expect(screen.getAllByRole("button", { name: "Try Again" })).toHaveLength(2));
  fireEvent.click(earlier);
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(2);
  expect(screen.getByText("#42 wasn’t changed: the account changed since.")).toBeTruthy();
});

it("settles an earlier workspace user's Try Again where the next has nothing to retry", async () => {
  const shell = await shellAnswering([refused]);
  const first = renderRow();
  act(() => first.hook.result.current.action!.run());
  const earlier = await screen.findByRole("button", { name: "Try Again" });
  first.hook.unmount();
  signInAs("user-2");
  await act(async () => shell.readAsHost(PR));
  renderRow();
  fireEvent.click(earlier);
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(screen.queryByText("Closing #42…")).toBeNull();
});

it("sends nothing from a toast once no list is open, and says why", async () => {
  const shell = await shellAnswering([changed("CLOSED")]);
  const first = renderRow();
  act(() => first.hook.result.current.action!.run());
  await screen.findByText("Closed #42 as @alice");
  first.hook.unmount();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(
    screen.getByText("#42 wasn’t changed back: the list it was made from isn’t open now."),
  ).toBeTruthy();
});

it("replaces an earlier workspace user's held Undo with why it sent nothing", async () => {
  const shell = installNamedWriteShell(asked, { extraWrites: WRITES });
  await shell.ready(PR);
  render(<Toaster />);
  const { hook, live } = renderRow();
  act(() => hook.result.current.action!.run());
  await screen.findByText("Closed #42 as @alice");
  live.state = "CLOSED";
  signInAs("user-2");
  await act(async () => shell.readAsHost(PR));
  hook.rerender();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(screen.getByText("#42 wasn’t changed back: the account changed since.")).toBeTruthy();
  expect(screen.queryByText("Reopening #42…")).toBeNull();
});

it("leaves no Checking toast once the workspace user changed during Check Again's read", async () => {
  const shell = await shellAnswering([
    { ok: false, error: { code: "outcome_unknown", message: "GitHub may have made it." } },
  ]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  const check = await screen.findByRole("button", { name: "Check Again" });
  const read = shell.pauseNextRead("pull_request_overview");
  await act(async () => {
    fireEvent.click(check);
    await read.started;
  });
  expect(screen.getByText("Checking #42…")).toBeTruthy();
  signInAs("user-2");
  hook.rerender();
  await act(async () => read.release());
  await waitFor(() => expect(screen.queryByText("Checking #42…")).toBeNull());
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
});

it("offers Check Again on the row while GitHub may have closed it, which reads and never resends", async () => {
  const shell = await shellAnswering([
    { ok: false, error: { code: "outcome_unknown", message: "GitHub may have made it." } },
  ]);
  const { hook } = renderRow();
  act(() => hook.result.current.action!.run());
  await screen.findByRole("button", { name: "Check Again" });
  hook.rerender();
  expect(hook.result.current.action).toMatchObject({
    id: "lifecycle:check-state-again",
    label: "Check Again",
    paletteLabel: "Check #42 Build Again",
  });
  const read = shell.pauseNextRead("pull_request_overview");
  await act(async () => {
    hook.result.current.action!.run();
    await read.started;
  });
  await act(async () => read.release());
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
});

it.each(["OPEN", "MERGED", null])(
  "keeps the row's Check Again while GitHub may have closed it, though the PR reads %s",
  async (state) => {
    await shellAnswering([
      { ok: false, error: { code: "outcome_unknown", message: "GitHub may have made it." } },
    ]);
    const { hook, live } = renderRow();
    act(() => hook.result.current.action!.run());
    await screen.findByRole("button", { name: "Check Again" });
    live.state = state;
    hook.rerender();
    expect(hook.result.current.action).toMatchObject({
      id: "lifecycle:check-state-again",
      label: "Check Again",
    });
  },
);
