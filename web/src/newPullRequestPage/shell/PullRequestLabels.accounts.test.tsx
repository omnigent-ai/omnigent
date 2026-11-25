// A refused label change made on this Mac shows only to the account and generation that
// made it: switching accounts, or signing in again as the same one, hides it, with no
// mutation event to say so.

import { render, screen } from "@testing-library/react";
import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterAll, beforeAll, beforeEach, expect, it, vi } from "vitest";

import {
  pullRequestLabelsMutationKey,
  type PullRequestLabel,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { draftOwner } from "@/lib/desktopPullRequestWrites";
import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";

import { PullRequestLabels } from "./PullRequestLabels";

const account = vi.hoisted(() => ({ login: "octo", generation: 1 }));
const PR_URL = "https://github.com/acme/app/pull/7";
const bug: PullRequestLabel = { name: "bug", color: "d73a4a", description: null };

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

/** A refused removal in the cache, as a Mac change made as `actor` leaves it. */
async function refusedAs(client: QueryClient, actor: { login: string; generation: number }) {
  const mutation = client.getMutationCache().build(client, {
    mutationKey: pullRequestLabelsMutationKey("conv_1"),
    mutationFn: () => Promise.reject(new Error("GitHub said no.")),
    retry: false,
  });
  await mutation
    .execute({
      prUrl: PR_URL,
      add: [],
      remove: [bug],
      madeBy: { owner: draftOwner(), reader: null },
      mac: { ticket: { route: { actor } } },
    })
    .catch(() => {});
}

function section(client: QueryClient) {
  return (
    <QueryClientProvider client={client}>
      <PullRequestLabels conversationId="conv_1" prUrl={PR_URL} labels={[bug]} editable />
    </QueryClientProvider>
  );
}

it("hides a refusal once the account or its generation moves, and doesn't bring it back", async () => {
  const client = new QueryClient();
  await refusedAs(client, { login: "octo", generation: 1 });
  const view = render(section(client));
  expect(screen.getByRole("alert")).toHaveTextContent(
    "Couldn’t remove the label bug: GitHub said no.",
  );

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
