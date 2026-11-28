// Comment drafts across their lifecycle in the Markdown composer: another workspace user,
// discarding a new line comment, switching pages, uploads across a reopen, attachment failures,
// and posting a reply or a line comment through the real form.

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type ReactNode, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";

const { currentUser, identityListeners, upload } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
  upload: {
    pending: [] as { resolve: (value: unknown) => void; reject: (error: unknown) => void }[],
  },
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));
vi.mock("@/newPullRequestPage/hooks/usePullRequests", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  uploadPullRequestAttachment: () =>
    new Promise((resolve, reject) => {
      upload.pending.push({ resolve, reject });
    }),
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  commentDraftKey,
  type PullRequestCommentDraft,
  usePullRequestCommentComposer,
  usePullRequestReviewThreadComposer,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  resetSettledUploadsForTests,
  resetUploadIdsForTests,
} from "@/markdownEditor/pullRequestAttachments";
import { PullRequestAttachmentsContext } from "@/pullRequestPage/PullRequestAttachmentsAvailability";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";

import { PullRequestCommentComposer, PullRequestCommentForm } from "./PullRequestCommentComposer";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR_URL = "https://github.com/acme/app/pull/7";
const ATTACHMENTS = { available: true, needsHost: false };

interface PendingRequest {
  path: string;
  body: Record<string, unknown>;
  respond: (response: Response) => void;
}

function stubFetch(): PendingRequest[] {
  const requests: PendingRequest[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const { pathname } = new URL(String(input), "http://localhost");
      return new Promise<Response>((respond) => {
        if (/\/github\/(comments|review-threads(\/[^/]+\/replies)?)$/.test(pathname))
          requests.push({ path: pathname, body: JSON.parse(String(init?.body)), respond });
        else respond(new Response("{}", { status: 404 }));
      });
    }),
  );
  return requests;
}

beforeEach(() => {
  // The workspace user's watcher stays subscribed across tests; it hears the reset too.
  currentUser.id = "user-1";
  for (const listener of identityListeners) listener();
  upload.pending = [];
  resetUploadIdsForTests();
  resetSettledUploadsForTests();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

/** The page around a comment form: React Query, tooltips, attachments, and the new page or not. */
function Page({
  client,
  pageV2,
  children,
}: {
  client: QueryClient;
  pageV2: boolean;
  children: ReactNode;
}) {
  const content = (
    <PullRequestAttachmentsContext.Provider value={ATTACHMENTS}>
      {children}
    </PullRequestAttachmentsContext.Provider>
  );
  return (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        {pageV2 ? <PullRequestImageScope prUrl={PR_URL}>{content}</PullRequestImageScope> : content}
      </TooltipProvider>
    </QueryClientProvider>
  );
}

function newClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function textboxNamed(name: string): HTMLTextAreaElement {
  // The new page's comment box starts folded to one line; open it the way a person would.
  const folded = screen.queryByRole("button", { name: "Add a comment…" });
  if (name === "New PR comment" && folded) fireEvent.click(folded);
  return screen.getByRole("textbox", { name }) as HTMLTextAreaElement;
}

/** A composer the draft store opens a moment after the click that asks for it. */
async function openedComposer(name: string): Promise<HTMLTextAreaElement> {
  return (await screen.findByRole("textbox", { name })) as HTMLTextAreaElement;
}

/** Type at the end of the composer, as a person would, so later edits land at the caret. */
function typeInto(textarea: HTMLTextAreaElement, text: string): void {
  act(() => textarea.focus());
  const value = textarea.value + text;
  fireEvent.change(textarea, { target: { value, selectionStart: value.length } });
  textarea.setSelectionRange(value.length, value.length);
}

function commandEnter(target: Element): void {
  act(() => {
    target.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
}

function attachFile(name: string): void {
  fireEvent.change(screen.getByTestId("markdown-composer-file-input"), {
    target: { files: [new File(["png"], name, { type: "image/png" })] },
  });
}

function draftOf(client: QueryClient, owner: string, threadId?: string) {
  return client.getQueryData<PullRequestCommentDraft>(
    commentDraftKey(owner, "conv_1", PR_URL, threadId),
  );
}

describe("another workspace user", () => {
  it("gets their own draft in a fresh composer, and the first user's text stays theirs", () => {
    stubFetch();
    const client = newClient();
    render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    typeInto(textboxNamed("New PR comment"), "User one private draft");
    expect(draftOf(client, "user-1")?.body).toBe("User one private draft");
    act(() => {
      currentUser.id = "user-2";
      for (const listener of identityListeners) listener();
    });
    expect(textboxNamed("New PR comment").value).toBe("");
    typeInto(textboxNamed("New PR comment"), "!");
    expect(draftOf(client, "user-2")?.body).toBe("!");
    expect(draftOf(client, "user-1")?.body).toBe("User one private draft");
  });
});

/** A new line comment the way the diff opens it: Select opens the form, Discard closes it. */
function NewLineComment() {
  const composer = usePullRequestReviewThreadComposer("conv_1", PR_URL, "src/app.ts", "a", "b");
  return (
    <>
      <button
        type="button"
        onClick={() => composer.selectLines({ side: "RIGHT", line: 3, start_line: null })}
      >
        Select
      </button>
      <button type="button" onClick={composer.cancel}>
        Discard
      </button>
      {composer.draft.anchor && (
        <PullRequestCommentForm {...composer} account={null} inline newThread />
      )}
    </>
  );
}

describe("discarding a new line comment", () => {
  async function openAndType(text: string): Promise<void> {
    fireEvent.click(screen.getByRole("button", { name: "Select" }));
    typeInto(await openedComposer("New review comment"), text);
  }

  async function reopenEmpty(): Promise<void> {
    fireEvent.click(screen.getByRole("button", { name: "Select" }));
    expect((await openedComposer("New review comment")).value).toBe("");
  }

  it("never brings the discarded text back", async () => {
    stubFetch();
    render(
      <Page client={newClient()} pageV2>
        <NewLineComment />
      </Page>,
    );
    await openAndType("Discard this comment");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
    await reopenEmpty();
  });

  it("keeps a discarded comment's late upload out of the next one", async () => {
    stubFetch();
    const client = newClient();
    render(
      <Page client={client} pageV2>
        <NewLineComment />
      </Page>,
    );
    await openAndType("Shot:");
    attachFile("late.png");
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
    await reopenEmpty();
    await act(async () => {
      upload.pending[0]!.resolve({
        url: "https://github.com/user-attachments/assets/late",
        name: "late.png",
        content_type: "image/png",
      });
    });
    expect(textboxNamed("New review comment").value).toBe("");
  });
});

describe("switching between the classic and the new page", () => {
  function Switchable({ client }: { client: QueryClient }) {
    const [pageV2, setPageV2] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setPageV2((value) => !value)}>
          Switch page
        </button>
        <Page client={client} pageV2={pageV2}>
          <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
        </Page>
      </>
    );
  }

  it("keeps the draft's text in both editors", () => {
    stubFetch();
    render(<Switchable client={newClient()} />);
    typeInto(textboxNamed("New PR comment"), "new page text");
    fireEvent.click(screen.getByRole("button", { name: "Switch page" }));
    const classic = textboxNamed("New PR comment");
    expect(screen.queryByRole("toolbar", { name: "Formatting" })).toBeNull();
    expect(classic.value).toBe("new page text");
    fireEvent.change(classic, { target: { value: "classic text" } });
    fireEvent.click(screen.getByRole("button", { name: "Switch page" }));
    expect(textboxNamed("New PR comment").value).toBe("classic text");
  });
});

describe("an upload across a reopen", () => {
  it("lands in the reopened composer, and Post waits for it", async () => {
    const requests = stubFetch();
    const client = newClient();
    const view = render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    typeInto(textboxNamed("New PR comment"), "Shot: ");
    attachFile("shot.png");
    expect(draftOf(client, "user-1")?.uploading).toBe(1);
    view.unmount();
    render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    expect(textboxNamed("New PR comment").value).toBe("Shot: ![Uploading shot.png…](#upload-1)");
    expect(screen.getByRole("button", { name: "Comment" })).toBeDisabled();
    commandEnter(textboxNamed("New PR comment"));
    expect(requests).toHaveLength(0);
    await act(async () => {
      upload.pending[0]!.resolve({
        url: "https://github.com/user-attachments/assets/shot",
        name: "shot.png",
        content_type: "image/png",
      });
    });
    expect(textboxNamed("New PR comment").value).toBe(
      "Shot: ![shot](https://github.com/user-attachments/assets/shot)",
    );
    expect(draftOf(client, "user-1")?.uploading).toBe(0);
    commandEnter(textboxNamed("New PR comment"));
    await waitFor(() => expect(requests).toHaveLength(1));
  });
});

describe("an attachment that fails", () => {
  it("keeps its message through edits and a reopen until dismissed", async () => {
    stubFetch();
    const client = newClient();
    const view = render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    typeInto(textboxNamed("New PR comment"), "Shot:");
    attachFile("shot.png");
    await act(async () => upload.pending[0]!.reject(new Error("offline")));
    const message = "Couldn’t attach shot.png: offline";
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(textboxNamed("New PR comment").value).toBe("Shot:");
    typeInto(textboxNamed("New PR comment"), " later");
    expect(screen.getByText(message)).toBeInTheDocument();
    view.unmount();
    render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    expect(await screen.findByText(message)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.queryByText(message)).toBeNull());
  });
});

describe("a batch with files that can't be attached", () => {
  /** An image File reporting `megabytes` as its size. */
  function imageOf(name: string, megabytes: number): File {
    const file = new File(["png"], name, { type: "image/png" });
    Object.defineProperty(file, "size", { value: megabytes * 1024 * 1024 });
    return file;
  }

  it.each([
    [
      "too large",
      imageOf("large.png", 11),
      "Couldn’t attach large.png: an image can be at most 10 MB.",
    ],
    [
      "not an image or video",
      new File(["notes"], "notes.txt", { type: "text/plain" }),
      "Only images and videos can be attached.",
    ],
  ])("says why a file was %s while the rest upload", async (_name, refused, message) => {
    stubFetch();
    render(
      <Page client={newClient()} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
    const box = textboxNamed("New PR comment");
    act(() => box.focus());
    fireEvent.change(screen.getByTestId("markdown-composer-file-input"), {
      target: { files: [refused, imageOf("small.png", 1)] },
    });
    expect(upload.pending).toHaveLength(1);
    expect(await screen.findByText(message)).toBeInTheDocument();
    await act(async () => {
      upload.pending[0]!.resolve({
        url: "https://github.com/user-attachments/assets/small",
        name: "small.png",
        content_type: "image/png",
      });
    });
    expect(screen.getByText(message)).toBeInTheDocument();
    // Choosing the next batch starts with no message from this one.
    fireEvent.change(screen.getByTestId("markdown-composer-file-input"), {
      target: { files: [imageOf("next.png", 1)] },
    });
    await waitFor(() => expect(screen.queryByText(message)).toBeNull());
  });
});

describe("posting through the composer", () => {
  it("posts a review reply", async () => {
    const requests = stubFetch();
    function Reply() {
      const composer = usePullRequestCommentComposer("conv_1", PR_URL, "thread_1");
      return <PullRequestCommentForm {...composer} account={null} inline />;
    }
    render(
      <Page client={newClient()} pageV2>
        <Reply />
      </Page>,
    );
    fireEvent.focus(screen.getByRole("textbox", { name: "Reply to review conversation" }));
    const reply = textboxNamed("Reply to review conversation");
    expect(reply.tagName).toBe("TEXTAREA");
    typeInto(reply, "Fixed in the next push");
    commandEnter(reply);
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]!.path).toMatch(/review-threads\/thread_1\/replies$/);
    expect(requests[0]!.body).toEqual({ pr_url: PR_URL, body: "Fixed in the next push" });
  });

  it("posts a new line comment with its anchor", async () => {
    const requests = stubFetch();
    render(
      <Page client={newClient()} pageV2>
        <NewLineComment />
      </Page>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Select" }));
    const comment = await openedComposer("New review comment");
    typeInto(comment, "Can this be const?");
    comment.setSelectionRange(12, 17);
    fireEvent.click(screen.getByRole("button", { name: "Bold" }));
    commandEnter(comment);
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]!.path).toMatch(/review-threads$/);
    expect(requests[0]!.body).toMatchObject({
      pr_url: PR_URL,
      body: "Can this be **const**?",
      anchor: { path: "src/app.ts", side: "RIGHT", line: 3 },
    });
  });
});

describe("the new page's folded comment box", () => {
  function renderBox(client: QueryClient) {
    return render(
      <Page client={client} pageV2>
        <PullRequestCommentComposer conversationId="conv_1" prUrl={PR_URL} account={null} />
      </Page>,
    );
  }
  const folded = () => screen.queryByRole("button", { name: /^(Add a comment…|Uploading|Shot)/ });

  it("opens per workspace user: another user's empty draft starts folded", () => {
    stubFetch();
    const client = newClient();
    renderBox(client);
    typeInto(textboxNamed("New PR comment"), "User one");
    act(() => {
      currentUser.id = "user-2";
      for (const listener of identityListeners) listener();
    });
    expect(screen.getByRole("button", { name: "Add a comment…" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "New PR comment" })).toBeNull();
    // Focus is elsewhere when the first user comes back; the restored draft mustn't take it.
    render(<button type="button">Elsewhere</button>);
    screen.getByRole("button", { name: "Elsewhere" }).focus();
    act(() => {
      currentUser.id = "user-1";
      for (const listener of identityListeners) listener();
    });
    expect(screen.getByRole("textbox", { name: "New PR comment" })).toHaveValue("User one");
    expect(screen.getByRole("button", { name: "Elsewhere" })).toHaveFocus();
  });

  it("keeps an upload that finishes while folded, and shows it on reopening", async () => {
    stubFetch();
    const client = newClient();
    renderBox(client);
    typeInto(textboxNamed("New PR comment"), "Shot: ");
    attachFile("shot.png");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("button", { name: "Uploading 1 file…" })).toBeInTheDocument();
    await act(async () => {
      upload.pending[0]!.resolve({
        url: "https://github.com/user-attachments/assets/shot",
        name: "shot.png",
        content_type: "image/png",
      });
    });
    expect(draftOf(client, "user-1")?.body).toBe(
      "Shot: ![shot](https://github.com/user-attachments/assets/shot)",
    );
    fireEvent.click(await screen.findByRole("button", { name: /^Shot:/ }));
    expect(textboxNamed("New PR comment").value).toBe(
      "Shot: ![shot](https://github.com/user-attachments/assets/shot)",
    );
  });

  it("shows an upload that fails while folded, and keeps the message on reopening", async () => {
    stubFetch();
    renderBox(newClient());
    textboxNamed("New PR comment");
    attachFile("shot.png");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await act(async () => upload.pending[0]!.reject(new Error("offline")));
    const message = "Couldn’t attach shot.png: offline";
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(folded()).toBeInTheDocument();
    fireEvent.click(folded()!);
    expect(screen.getByText(message)).toBeInTheDocument();
  });
});
