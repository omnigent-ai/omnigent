// Removing this Mac's own account as a reviewer takes the PR out of the Pull Requests page's
// Review requests, so the section asks the page to show where it went; any other reviewer
// change sends nothing to the page.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterAll, afterEach, beforeAll, expect, it, vi } from "vitest";

import { pullRequestReviewersMutationKey } from "@/newPullRequestPage/hooks/usePullRequests";
import type * as DesktopPullRequestWrites from "@/lib/desktopPullRequestWrites";
import { PullRequestActionOriginContext } from "@/lib/pullRequestActionOrigin";

import { PullRequestReviewers } from "./PullRequestReviewers";

const PR_URL = "https://github.com/acme/app/pull/7";
const ACTOR = { login: "octo", generation: 1 };

vi.mock("@/lib/desktopPullRequestWrites", async (importOriginal) => {
  const actual = await importOriginal<typeof DesktopPullRequestWrites>();
  const { mockedMacRoute } = await import("@/test/macWriteRouteMock");
  return {
    ...actual,
    ...mockedMacRoute(actual, (url) => ({
      target: { provider: "github", host: "github.com", repository: "acme/app", number: 7, url },
      actor: { login: "octo", generation: 1 },
      reader: null,
    })),
    macWriteTicketCurrent: () => true,
    useMacWriteRouteVersion: () => "octo:1",
  };
});

beforeAll(() => notifyManager.setScheduler((callback) => callback()));
afterAll(() => notifyManager.setScheduler((callback) => setTimeout(callback, 0)));
afterEach(() => {
  cleanup();
  origin.begin.mockClear();
});

const origin = { begin: vi.fn(() => ({ succeeded: vi.fn() })) };

function renderSection(client = new QueryClient()) {
  render(
    <QueryClientProvider client={client}>
      <PullRequestActionOriginContext.Provider value={origin}>
        <PullRequestReviewers
          conversationId="conv_1"
          prUrl={PR_URL}
          reviewers={[
            { login: "octo", kind: "user", state: "PENDING" },
            { login: "core", kind: "team", state: "PENDING" },
          ]}
          author="dev"
          editable
        />
      </PullRequestActionOriginContext.Provider>
    </QueryClientProvider>,
  );
}

it("asks the page to show where the PR went when you withdraw yourself", () => {
  renderSection();
  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from octo" }));
  expect(origin.begin).toHaveBeenCalledExactlyOnceWith(PR_URL, ACTOR, {
    landing: "removed_you",
  });
});

it("sends nothing to the page when you withdraw someone else", () => {
  renderSection();
  fireEvent.click(screen.getByRole("button", { name: "Withdraw review request from Team core" }));
  expect(origin.begin).not.toHaveBeenCalled();
});

it("asks again on Retry after GitHub refused withdrawing you", async () => {
  const client = new QueryClient();
  const mutation = client.getMutationCache().build(client, {
    mutationKey: pullRequestReviewersMutationKey("conv_1"),
    mutationFn: () => Promise.reject(new Error("GitHub said no.")),
    retry: false,
  });
  await mutation
    .execute({
      prUrl: PR_URL,
      add: [],
      remove: [{ login: "octo", kind: "user" }],
      mac: { ticket: { route: { actor: ACTOR } } },
    })
    .catch(() => {});
  renderSection(client);
  fireEvent.click(screen.getByRole("button", { name: "Retry" }));
  expect(origin.begin).toHaveBeenCalledExactlyOnceWith(PR_URL, ACTOR, {
    landing: "removed_you",
  });
});
