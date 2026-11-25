// A row's Close Pull Request on the Pull Requests page while it reads as one of this Mac's
// logins: it closes at once, only as that account, and its Undo goes only as the same account.
// Switching the page's account before Undo, in either direction, or a shell that can't write
// as the login, sends nothing, as gh's active account or by name.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { type ReactNode, useMemo } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { hostFetch, toast } = vi.hoisted(() => ({ hostFetch: vi.fn(), toast: vi.fn() }));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(toast, { success: vi.fn(), error: vi.fn(), dismiss: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  resetDesktopPullRequestWritesForTests,
  setLifecycleFlagForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { type MacNamedReader, resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { resetStateChainsForTests, stateChainKey } from "@/pullRequestPage/pullRequestStateChain";
import {
  captureMacChange,
  startStateChange,
  useStateChangeSend,
} from "@/pullRequestPage/usePullRequestStateAction";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import type { PullRequestListEntry } from "./PullRequestListRows";
import { useRowStateChanges } from "./useRowStateChanges";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "pull-requests-page";
const CLOSE_WRITES = ["pull_request_close", "pull_request_reopen"];
const closedAs = (login: string) => ({
  ok: true,
  data: { object: "session.github.pr_state", pr_url: PR, state: "CLOSED" },
  actor: { login },
});
const ENTRY = {
  association: { url: PR, repository: "acme/app", number: 42, provider: "github" },
  row: { url: PR, repository: "acme/app", number: 42, title: "Ship it" },
  pr: { url: PR, state: "OPEN", is_draft: false },
} as unknown as PullRequestListEntry;

/** The page's account, as its read-as menu sets it; a rerender shows the change. */
const page: { reader: MacNamedReader | null } = { reader: null };

function renderRow() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const Wrapper = ({ children }: { children: ReactNode }) => {
    const reader = page.reader;
    const readAsValue = useMemo(() => ({ reader }), [reader]);
    return (
      <QueryClientProvider client={client}>
        <MacReadAsContext.Provider value={readAsValue}>{children}</MacReadAsContext.Provider>
      </QueryClientProvider>
    );
  };
  const rows = renderHook(
    () => {
      const changes = useRowStateChanges({
        conversationId: SESSION,
        entriesByUrl: new Map([[PR, ENTRY]]),
        onOpen: () => {},
      });
      return { ...changes, action: changes.actionFor(ENTRY, "#42") };
    },
    { wrapper: Wrapper },
  );
  /** The page now reads as `reader`; the list renders again. */
  const readAs = (reader: MacNamedReader | null) => {
    page.reader = reader;
    rows.rerender();
  };
  const close = () => act(() => rows.result.current.action?.run());
  /** The Undo the last toast offers, once the close was made. */
  const undo = async () => {
    await waitFor(() => expect(String(toast.mock.calls.at(-1)?.[0])).toMatch(/^Closed/));
    const options = toast.mock.calls.at(-1)![1] as { action: { onClick: () => void } };
    return () => act(() => options.action.onClick());
  };
  return { rows, readAs, close, undo, wrapper: Wrapper };
}

function expectNothingSent(shell: ReturnType<typeof installNamedWriteShell>) {
  expect(shell.codeHostWrite).not.toHaveBeenCalled();
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
  expect(hostFetch).not.toHaveBeenCalled();
}

beforeEach(() => {
  page.reader = null;
  toast.mockReset();
  resetStateChainsForTests();
  setLifecycleFlagForTests(true);
});

afterEach(() => {
  hostFetch.mockReset();
  setLifecycleFlagForTests(null);
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("a row's Close Pull Request as the page's account", () => {
  it("closes at once as the login the page reads as, and says so", async () => {
    const shell = installNamedWriteShell(() => closedAs("bob"), { extraWrites: CLOSE_WRITES });
    await shell.ready(PR);
    page.reader = BOB;
    const { close, undo } = renderRow();
    close();
    await waitFor(() => expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_close",
      macLogin: "bob",
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    await undo();
    expect(toast.mock.calls.at(-1)![0]).toBe("Closed #42 as @bob");
  });

  it.each([
    ["Automatic to a login by name", null, BOB],
    ["a login by name to Automatic", BOB, null],
  ] as const)(
    "sends no Undo once the page switched from %s, even once switched back",
    async (_label, from, to) => {
      const login = from ? "bob" : "alice";
      const shell = installNamedWriteShell(() => closedAs(login), { extraWrites: CLOSE_WRITES });
      await shell.ready(PR);
      page.reader = from;
      const { close, undo, readAs } = renderRow();
      close();
      const runUndo = await undo();
      const writes = () =>
        shell.codeHostWrite.mock.calls.length + shell.codeHostWriteAs.mock.calls.length;
      expect(writes()).toBe(1);
      readAs(to);
      runUndo();
      await act(async () => {});
      expect(toast.mock.calls.at(-1)![0]).toBe(
        "#42 wasn’t changed back: the account changed since.",
      );
      expect(writes()).toBe(1);
    },
  );

  it("sends no Undo once the login signed in again since the close", async () => {
    const shell = installNamedWriteShell(() => closedAs("bob"), { extraWrites: CLOSE_WRITES });
    await shell.ready(PR);
    page.reader = BOB;
    const { close, undo } = renderRow();
    close();
    const runUndo = await undo();
    shell.bobSignsInAgain(7);
    await shell.readAsBob(PR);
    runUndo();
    await act(async () => {});
    expect(shell.codeHostWriteAs).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["an older shell without writes by name", { writeAs: false }],
    ["a partial bridge without the method", { writeAsMethod: false }],
  ] as const)("offers nothing as a login by name on %s", async (_label, options) => {
    const shell = installNamedWriteShell(() => closedAs("alice"), {
      extraWrites: CLOSE_WRITES,
      ...options,
    });
    await shell.ready(PR);
    page.reader = BOB;
    const { rows } = renderRow();
    expect(rows.result.current.action).toBeNull();
    expectNothingSent(shell);
  });
});

describe("a state change once the page reads as another account than its click", () => {
  it.each([
    ["Automatic to a login by name", null, BOB],
    ["a login by name to Automatic", BOB, null],
  ] as const)("is skipped from %s, sending nothing", async (_label, from, to) => {
    const shell = installNamedWriteShell(() => closedAs("alice"), { extraWrites: CLOSE_WRITES });
    await shell.ready(PR);
    page.reader = from;
    const { wrapper } = renderRow();
    const send = renderHook(() => useStateChangeSend(SESSION), { wrapper }).result.current;
    const capture = captureMacChange(PR, "user-1", "pull_request_close", from)!;
    expect(capture).not.toBeNull();
    const outcome = await startStateChange({
      key: stateChainKey("user-1", PR),
      owner: "user-1",
      capture,
      send,
      // The page moved on after the click; the mutations rendered at the click still route as before.
      currentReader: () => to,
      origin: null,
    });
    expect(outcome).toMatchObject({ kind: "skipped", reason: "changed" });
    expectNothingSent(shell);
  });
});
