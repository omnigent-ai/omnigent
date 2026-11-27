// The PR comment box while its post is on its way: it shows Posting… over the sent text, a
// repeated ⌘⏎ sends nothing more, and a failed post leaves the text to edit and send again.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const { hostFetch } = vi.hoisted(() => ({ hostFetch: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));
vi.mock("./PullRequestDetails", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  PullRequestMarkdownPreview: () => null,
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { PullRequestAttachmentsContext } from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";

import { PullRequestCommentComposer } from "./PullRequestCommentComposer";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const ATTACHMENTS = { available: true, needsHost: false };

let posts: unknown[];
let answer!: (response: Response) => void;

beforeEach(() => {
  posts = [];
  hostFetch.mockReset();
  hostFetch.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const { pathname } = new URL(String(input), "http://localhost");
    if (pathname.endsWith("/github/comments")) {
      posts.push(JSON.parse(String(init?.body)).body);
      return new Promise<Response>((resolve) => {
        answer = resolve;
      });
    }
    return Promise.resolve(new Response("{}", { status: 404 }));
  });
});

afterEach(cleanup);

function renderBox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestImageScope prUrl={PR_URL}>
          <PullRequestAttachmentsContext.Provider value={ATTACHMENTS}>
            <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
          </PullRequestAttachmentsContext.Provider>
        </PullRequestImageScope>
      </TooltipProvider>
    </QueryClientProvider>,
  );
  const folded = screen.queryByRole("button", { name: "Add a comment…" });
  if (folded) fireEvent.click(folded);
  const box = screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement;
  fireEvent.change(box, { target: { value: "Looks good." } });
  return () => screen.getByRole("textbox", { name: "New PR comment" }) as HTMLTextAreaElement;
}

describe("a comment on its way", () => {
  it("shows Posting… over its text, and a repeated ⌘⏎ sends nothing more", async () => {
    const box = renderBox();
    fireEvent.keyDown(box(), { key: "Enter", ctrlKey: true });
    fireEvent.keyDown(box(), { key: "Enter", ctrlKey: true });
    await screen.findByRole("button", { name: "Posting…" });
    expect(screen.getByRole("button", { name: "Posting…" })).toBeDisabled();
    expect(box().value).toBe("Looks good.");
    expect(box()).toBeDisabled();
    fireEvent.keyDown(box(), { key: "Enter", ctrlKey: true });
    fireEvent.submit(box().closest("form")!);
    await act(async () => {});
    expect(posts).toEqual(["Looks good."]);
  });

  it("leaves a refused comment's text to edit and send again, saying why", async () => {
    const box = renderBox();
    fireEvent.keyDown(box(), { key: "Enter", ctrlKey: true });
    await screen.findByRole("button", { name: "Posting…" });
    await act(async () =>
      answer(new Response(JSON.stringify({ detail: "Body is too long" }), { status: 422 })),
    );
    await waitFor(() => expect(box()).not.toBeDisabled());
    expect(box().value).toBe("Looks good.");
    expect(await screen.findByText(/Body is too long/)).toBeInTheDocument();
    fireEvent.keyDown(box(), { key: "Enter", ctrlKey: true });
    await waitFor(() => expect(posts).toEqual(["Looks good.", "Looks good."]));
  });
});
