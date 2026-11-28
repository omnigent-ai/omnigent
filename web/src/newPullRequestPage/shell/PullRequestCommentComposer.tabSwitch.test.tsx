// The Overview and Comments tabs each mount their own composer for one shared draft. A switch
// right after a keystroke must open the incoming composer on that keystroke, and post it.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));
vi.mock("./PullRequestDetails", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  PullRequestMarkdownPreview: () => null,
}));

import { Tabs, TabsContent } from "@/components/ui/tabs";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  commentDraftKey,
  type PullRequestCommentDraft,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestAttachmentsContext } from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";

import { PullRequestCommentComposer } from "./PullRequestCommentComposer";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const BOX = "New PR comment";
const ATTACHMENTS = { available: true, needsHost: false };
type Tab = "summary" | "comments";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

/** Captures each posted comment's body; every other request fails, as an offline host would. */
function capturePosts(): unknown[] {
  const bodies: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { pathname } = new URL(String(input), "http://localhost");
      if (pathname.endsWith("/github/comments")) {
        bodies.push(JSON.parse(String(init?.body)).body);
        return new Promise<Response>(() => {});
      }
      return Promise.resolve(new Response("{}", { status: 404 }));
    }),
  );
  return bodies;
}

/** The page's two tabs, switched only by their controlled value; each mounts the same form. */
function TwoTabs({ client, tab, form }: { client: QueryClient; tab: Tab; form: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <PullRequestImageScope prUrl={PR_URL}>
          <PullRequestAttachmentsContext.Provider value={ATTACHMENTS}>
            <Tabs value={tab}>
              <TabsContent value="summary">
                <div>{form}</div>
              </TabsContent>
              <TabsContent value="comments">{form}</TabsContent>
            </Tabs>
          </PullRequestAttachmentsContext.Provider>
        </PullRequestImageScope>
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function commentBox(): HTMLTextAreaElement {
  // The comment box starts folded to one line; open it the way a person would.
  const folded = screen.queryByRole("button", { name: "Add a comment…" });
  if (folded) fireEvent.click(folded);
  return screen.getByRole("textbox", { name: BOX }) as HTMLTextAreaElement;
}

function typeAtEnd(textarea: HTMLTextAreaElement, text: string): void {
  const value = textarea.value + text;
  fireEvent.change(textarea, { target: { value, selectionStart: value.length } });
}

describe("a quick switch between Overview and Comments", () => {
  it.each<[Tab, Tab]>([
    ["summary", "comments"],
    ["comments", "summary"],
  ])(
    "keeps the last keystrokes when the box moves from %s to %s, and posts them",
    async (from, to) => {
      const bodies = capturePosts();
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      const form = (
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      );
      const view = render(<TwoTabs client={client} tab={from} form={form} />);

      const outgoing = commentBox();
      act(() => outgoing.focus());
      typeAtEnd(outgoing, "before");
      await act(async () => {
        await new Promise((resolve) => {
          setTimeout(resolve, 200);
        });
      });
      act(() => typeAtEnd(outgoing, " AFTER"));
      view.rerender(<TwoTabs client={client} tab={to} form={form} />);

      const incoming = commentBox();
      expect(incoming).not.toBe(outgoing);
      expect(incoming.value).toBe("before AFTER");
      const draft = client.getQueryData<PullRequestCommentDraft>(
        commentDraftKey("user-1", "conv_1", PR_URL),
      );
      expect(draft?.body).toBe("before AFTER");

      fireEvent.submit(incoming.closest("form")!);
      await waitFor(() => expect(bodies).toEqual(["before AFTER"]));
    },
  );
});
