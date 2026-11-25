// A refused reviewer change made on this Mac shows only to the account and generation that
// made it: switching accounts, or signing in again as the same one, hides it, with no
// mutation event to say so.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterAll, beforeAll, beforeEach, expect, it, vi } from "vitest";

import { pullRequestReviewersMutationKey } from "@/newPullRequestPage/hooks/usePullRequests";
import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";

import { useShowsMacAccount, VisibleMacAccountsProvider } from "../../shell/MacWriteNotices";
import { PullRequestReviewers } from "./PullRequestReviewers";

const account = vi.hoisted(() => ({ login: "octo", generation: 1 }));
const PR_URL = "https://github.com/acme/app/pull/7";

vi.mock("@/lib/desktopPullRequestWrites", async (importOriginal) => {
  const actual = await importOriginal<typeof DesktopPullRequestWrites>();
  const { mockedMacRoute } = await import("@/test/macWriteRouteMock");
  return {
    ...actual,
    ...mockedMacRoute(actual, (url) => ({
      target: { provider: "github", host: "github.com", repository: "acme/app", number: 7, url },
      actor: { ...account },
      reader: null,
    })),
    macWriteTicketCurrent: () => true,
    useMacWriteRouteVersion: () => `${account.login}:${account.generation}`,
  };
});

beforeAll(() => notifyManager.setScheduler((callback) => callback()));
afterAll(() => notifyManager.setScheduler((callback) => setTimeout(callback, 0)));
beforeEach(() => Object.assign(account, { login: "octo", generation: 1 }));

/** A refused withdrawal in the cache, as a Mac change made as `actor` leaves it. */
async function refusedAs(client: QueryClient, actor: { login: string; generation: number }) {
  const mutation = client.getMutationCache().build(client, {
    mutationKey: pullRequestReviewersMutationKey("conv_1"),
    mutationFn: () => Promise.reject(new Error("GitHub said no.")),
    retry: false,
  });
  await mutation
    .execute({
      prUrl: PR_URL,
      add: [],
      remove: [{ login: "core", kind: "team" }],
      mac: { ticket: { route: { actor } } },
    })
    .catch(() => {});
}

function section(client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <PullRequestReviewers
        conversationId="conv_1"
        prUrl={PR_URL}
        reviewers={[{ login: "core", kind: "team", state: "PENDING" }]}
        author="dev"
        editable
      />
    </QueryClientProvider>
  );
}

it("hides a refusal once the account or its generation moves, and doesn't bring it back", async () => {
  const client = new QueryClient();
  await refusedAs(client, { login: "octo", generation: 1 });
  const view = render(section(client));
  expect(screen.getByRole("alert")).toHaveTextContent("GitHub said no.");

  Object.assign(account, { login: "octo", generation: 2 });
  view.rerender(section(client));
  expect(screen.queryByRole("alert")).toBeNull();

  Object.assign(account, { login: "hubot", generation: 3 });
  view.rerender(section(client));
  expect(screen.queryByRole("alert")).toBeNull();
  Object.assign(account, { login: "octo", generation: 4 });
  view.rerender(section(client));
  expect(screen.queryByRole("alert")).toBeNull();
});

function Toolbar({ login }: { login: string | null }) {
  useShowsMacAccount(login ? { host: "github.com", login } : null);
  return null;
}

/** The reviewer picker, open, on a page whose toolbar names `toolbarLogin`. */
function openPickerUnder(toolbarLogin: string | null) {
  const client = new QueryClient();
  render(
    <VisibleMacAccountsProvider>
      <Toolbar login={toolbarLogin} />
      {section(client)}
    </VisibleMacAccountsProvider>,
  );
  fireEvent.click(screen.getByRole("button", { name: "Request reviewers" }));
}

it("leaves out who changes are made as where the page already names that account", () => {
  openPickerUnder("OCTO");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(screen.queryByText(/Changes are made as/)).toBeNull();
});

it("says who changes are made as where the page names another account, or none", () => {
  openPickerUnder("someone-else");
  expect(screen.getByText("Changes are made as @octo")).toBeInTheDocument();
  cleanup();
  openPickerUnder(null);
  expect(screen.getByText("Changes are made as @octo")).toBeInTheDocument();
});
