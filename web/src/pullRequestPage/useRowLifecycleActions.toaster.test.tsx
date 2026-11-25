// A row's Ready for review and Convert to draft through the real Toaster, as the Pull Requests
// page replaces its table on each tab while the toasts stay: Undo and Try Again run through
// the list mounted now, as the account it reads as, and send nothing once that changed or no
// list is mounted. Each send checks the PR as the list shows it then.

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

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
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
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { pullRequestChangeInFlight } from "./pullRequestChangeInFlight";
import { resetLifecycleActionsForTests } from "./pullRequestLifecycleActions";
import { resetStateChainsForTests, stateChainKey } from "./pullRequestStateChain";
import { type LifecycleRow, useRowLifecycleActions } from "./useRowLifecycleActions";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "pull-requests-page";
const KEY = stateChainKey("user-1", PR);

const row = (state: "OPEN" | "CLOSED", isDraft: boolean): LifecycleRow => ({
  url: PR,
  number: 42,
  paletteName: "#42",
  pr: { state, is_draft: isDraft, head_sha: "a".repeat(40) },
});
const READY = row("OPEN", false);
const DRAFT = row("OPEN", true);
const answer = (login: string) => (request: { operation?: unknown }) => ({
  ok: true,
  data: {
    object: "session.github.pr_draft",
    pr_url: PR,
    is_draft: request.operation === "pull_request_convert_to_draft",
  },
  actor: { login },
});
const AUTOMATIC = { reader: null };
const AS_BOB = { reader: BOB };
/** The page's, which each table it mounts recovers its toasts through. */
const RECOVERY = "pull-requests-page-recovery";
const refused = { ok: false, error: { code: "rejected", message: "Refused." } };

/** A table on the page reading as `reader`, listing `rows` until told otherwise. */
function renderTable(rows: LifecycleRow[], reader: typeof BOB | null = null) {
  let shown = rows;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const readAs = reader ? AS_BOB : AUTOMATIC;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={readAs}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  const hook = renderHook(
    () => {
      const listed = new Map(shown.map((listedRow) => [listedRow.url, listedRow]));
      const actions = useRowLifecycleActions(SESSION, true, listed, RECOVERY);
      return shown.flatMap((listedRow) => actions.actionsFor(listedRow));
    },
    { wrapper },
  );
  return {
    hook,
    /** The list now shows `next`. */
    list: (next: LifecycleRow[]) => {
      shown = next;
      hook.rerender();
    },
  };
}

const writes = (shell: ReturnType<typeof installNamedWriteShell>) =>
  [...shell.codeHostWrite.mock.calls, ...shell.codeHostWriteAs.mock.calls].map(
    ([request]) => (request as { operation: string }).operation,
  );

/** Past any send a click could still start. */
const settle = () =>
  act(async () => {
    await new Promise((resolve) => {
      setTimeout(resolve, 200);
    });
  });

async function shellAnswering(
  answers: (request: { operation?: unknown; macLogin?: unknown }) => unknown,
) {
  const shell = installNamedWriteShell(answers, { extraWrites: LIFECYCLE_WRITE_OPERATIONS });
  await shell.ready(PR);
  setLifecycleFlagForTests(true);
  render(<Toaster />);
  return shell;
}

beforeEach(() => {
  toast.dismiss();
  resetStateChainsForTests();
  resetLifecycleActionsForTests();
});

afterEach(() => {
  toast.dismiss();
  cleanup();
  setLifecycleFlagForTests(null);
  resetStateChainsForTests();
  resetLifecycleActionsForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

it("undoes through the table that replaced it, as the same account", async () => {
  const shell = await shellAnswering(answer("alice"));
  const first = renderTable([READY]);
  act(() => first.hook.result.current[0].run());
  await screen.findByText("Converted #42 to a draft as @alice");
  first.hook.unmount();
  renderTable([DRAFT]);
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() =>
    expect(writes(shell)).toEqual([
      "pull_request_convert_to_draft",
      "pull_request_ready_for_review",
    ]),
  );
  expect(await screen.findByText("Marked #42 ready for review as @alice")).toBeTruthy();
});

it.each([
  ["Automatic to a login by name", null, BOB],
  ["a login by name to Automatic", BOB, null],
] as const)(
  "sends no Undo once the table that replaced it reads from %s",
  async (_label, from, to) => {
    const shell = await shellAnswering(answer(from ? "bob" : "alice"));
    const first = renderTable([READY], from);
    act(() => first.hook.result.current[0].run());
    await screen.findByText(/^Converted #42 to a draft as @/);
    first.hook.unmount();
    renderTable([DRAFT], to);
    fireEvent.click(screen.getByRole("button", { name: "Undo" }));
    await settle();
    expect(writes(shell)).toEqual(["pull_request_convert_to_draft"]);
    expect(screen.getByText("#42 wasn’t changed back: the account changed since.")).toBeTruthy();
  },
);

it("sends no Try Again once the table that replaced it reads as another login", async () => {
  const shell = await shellAnswering(() => refused);
  const first = renderTable([DRAFT]);
  act(() => first.hook.result.current[0].run());
  const tryAgain = await screen.findByRole("button", { name: "Try Again" });
  first.hook.unmount();
  renderTable([DRAFT], BOB);
  fireEvent.click(tryAgain);
  await settle();
  expect(writes(shell)).toEqual(["pull_request_ready_for_review"]);
});

it("runs a list's own menu as its own account while another list is mounted", async () => {
  const shell = await shellAnswering((request) =>
    answer(typeof request.macLogin === "string" ? request.macLogin : "alice")(request),
  );
  const first = renderTable([READY]);
  renderTable([READY], BOB);
  act(() => first.hook.result.current[0].run());
  await settle();
  expect(shell.codeHostWrite).toHaveBeenCalledTimes(1);
  expect(shell.codeHostWriteAs).not.toHaveBeenCalled();
});

it("sends nothing from a toast once no list is open, and says why", async () => {
  const shell = await shellAnswering(answer("alice"));
  const first = renderTable([READY]);
  act(() => first.hook.result.current[0].run());
  await screen.findByText("Converted #42 to a draft as @alice");
  first.hook.unmount();
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await settle();
  expect(writes(shell)).toEqual(["pull_request_convert_to_draft"]);
  expect(
    screen.getByText("#42 wasn’t changed back: the list it was made from isn’t open now."),
  ).toBeTruthy();
});

it("undoes a conversion whose row left the list before it read as a draft", async () => {
  const shell = await shellAnswering(answer("alice"));
  const table = renderTable([READY]);
  act(() => table.hook.result.current[0].run());
  await screen.findByText("Converted #42 to a draft as @alice");
  // Filtered out, or taken out by its source, without first showing the draft.
  table.list([]);
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await waitFor(() =>
    expect(writes(shell)).toEqual([
      "pull_request_convert_to_draft",
      "pull_request_ready_for_review",
    ]),
  );
});

it("sends no Undo while the row is listed in a state the inverse doesn't take", async () => {
  const shell = await shellAnswering(answer("alice"));
  const table = renderTable([READY]);
  act(() => table.hook.result.current[0].run());
  await screen.findByText("Converted #42 to a draft as @alice");
  table.list([row("CLOSED", true)]);
  fireEvent.click(screen.getByRole("button", { name: "Undo" }));
  await settle();
  expect(writes(shell)).toEqual(["pull_request_convert_to_draft"]);
  expect(screen.getByText(/^#42 wasn’t changed back: it isn’t an open draft/)).toBeTruthy();
});

it.each([
  ["Convert to draft", READY, row("CLOSED", false)],
  ["Convert to draft", READY, DRAFT],
  ["Ready for review", DRAFT, row("CLOSED", true)],
  ["Ready for review", DRAFT, READY],
] as const)(
  "sends no %s kept from an earlier render once the PR no longer takes it",
  async (_label, offeredOn, nowListed) => {
    const shell = await shellAnswering(answer("alice"));
    const table = renderTable([offeredOn]);
    const kept = table.hook.result.current[0];
    table.list([nowListed]);
    act(() => kept.run());
    await settle();
    expect(writes(shell)).toEqual([]);
    expect(screen.getByText(/^#42 wasn’t changed: it isn’t/)).toBeTruthy();
  },
);

it("keeps a conversion running once its table goes, and says what it came to", async () => {
  let release!: (value: unknown) => void;
  const pending = new Promise((resolve) => {
    release = resolve;
  });
  const shell = await shellAnswering(() => pending);
  const table = renderTable([READY]);
  act(() => table.hook.result.current[0].run());
  await waitFor(() => expect(writes(shell)).toHaveLength(1));
  expect(pullRequestChangeInFlight(KEY)?.label).toBe("Converting…");
  table.hook.unmount();
  await act(async () => release(answer("alice")({ operation: "pull_request_convert_to_draft" })));
  expect(await screen.findByText("Converted #42 to a draft as @alice")).toBeTruthy();
  expect(pullRequestChangeInFlight(KEY)).toBeNull();
});
