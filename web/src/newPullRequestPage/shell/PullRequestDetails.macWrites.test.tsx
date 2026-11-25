// The title and description editors on the Mac write path, rendered with the real hooks
// and stores over a stand-in shell: one field at a time, only over the value the editor
// started from, and each workspace user's editor kept their own.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, hostFetch, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  hostFetch: vi.fn(),
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: hostFetch,
}));

import {
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { installMacWriteShell, readAsMac, removeMacWriteShell } from "@/test/macWriteShell";
import {
  PullRequestDescription,
  PullRequestTitle,
  resetPullRequestDescriptionDrafts,
} from "./PullRequestDetails";

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function signInAs(id: string | null) {
  act(() => {
    currentUser.id = id;
    for (const listener of [...identityListeners]) listener();
  });
}

const saved = (title: string, body: string) => ({
  ok: true,
  data: { object: "session.github.pr_details", title, body },
  actor: { login: "mac-octo" },
});
const changed = (field: "title" | "body", value: string) => ({
  ok: false,
  error: {
    code: "pull_request_changed",
    message: `This ${field === "title" ? "title" : "description"} changed on GitHub since you started editing.`,
  },
  current: { field, value },
});
const json = (value: unknown) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });

function renderDetails({
  title = "Old title",
  body = "Old body",
  attachments = false,
  attachmentsNeedHost = false,
} = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return render(
    <>
      <PullRequestTitle conversationId={SESSION} prUrl={PR} title={title} editable />
      <PullRequestDescription
        conversationId={SESSION}
        prUrl={PR}
        body={body}
        editable
        attachments={attachments}
        attachmentsNeedHost={attachmentsNeedHost}
      />
    </>,
    { wrapper },
  );
}

function editTitle(value: string) {
  fireEvent.click(screen.getByRole("button", { name: "Edit title" }));
  fireEvent.change(screen.getByRole("textbox", { name: "Pull request title" }), {
    target: { value },
  });
  fireEvent.click(screen.getByRole("button", { name: "Save" }));
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  resetPullRequestDescriptionDrafts();
  hostFetch.mockReset();
});

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetPullRequestDescriptionDrafts();
  removeMacWriteShell();
});

describe("the title on this Mac", () => {
  it("sends only the title, with the value the editor started from, and closes once saved", async () => {
    const { write } = await installMacWriteShell(PR, async () => saved("New title", "Old body"));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    renderDetails();
    editTitle("  New title ");
    expect(write.mock.calls[0][0]).toMatchObject({
      operation: "pull_request_details_update",
      params: { field: "title", value: "New title", expected: "Old title" },
    });
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Pull request title" })).toBeNull(),
    );
    expect(hostFetch).not.toHaveBeenCalled();
  });

  it("shows what GitHub has now when the title changed there, and Use theirs closes the editor", async () => {
    const { write } = await installMacWriteShell(PR, async () => changed("title", "Their title"));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    renderDetails();
    editTitle("My title");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This title changed on GitHub since you started editing.");
    expect(alert).toHaveTextContent("Their title");
    // The edit is kept, as typed.
    expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue("My title");
    fireEvent.click(screen.getByRole("button", { name: "Use theirs" }));
    expect(screen.queryByRole("textbox", { name: "Pull request title" })).toBeNull();
    expect(write).toHaveBeenCalledOnce();
  });

  it("gives the editor back to its user after A to B and back, saying it may have been saved", async () => {
    const answer = deferred<unknown>();
    const { write } = await installMacWriteShell(PR, () => answer.promise);
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    renderDetails();
    editTitle("My title");
    signInAs("user-2");
    // User 2 sees the title, not user 1's editor.
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Pull request title" })).toBeNull(),
    );
    expect(screen.getByRole("heading", { name: "Old title" })).toBeInTheDocument();
    signInAs("user-1");
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue("My title"),
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "This may have been saved before you switched accounts.",
    );
    // The old answer changes nothing, and nothing is sent again.
    await act(async () => answer.resolve(saved("My title", "Old body")));
    expect(screen.getByRole("textbox", { name: "Pull request title" })).toHaveValue("My title");
    expect(write).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
    readAsMac();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(write).toHaveBeenCalledTimes(2);
  });
});

describe("the description on this Mac", () => {
  it("says attaching needs the session's machine where it can't upload", async () => {
    await installMacWriteShell(PR, async () => ({}));
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    renderDetails({ attachmentsNeedHost: true });
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    const attach = screen.getByRole("button", { name: "Attach images or videos" });
    expect(attach).toBeDisabled();
    expect(attach).toHaveAttribute("title", "Attaching files needs the session’s machine");
  });

  it("finishes an upload into its own user's draft only", async () => {
    await installMacWriteShell(PR, async () => ({}));
    const upload = deferred<Response>();
    hostFetch.mockImplementation(() => upload.promise);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => "blob:copy" }));
    renderDetails({ attachments: true });
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    const file = new File(["png"], "shot.png", { type: "image/png" });
    fireEvent.change(screen.getByTestId("pull-request-description-file-input"), {
      target: { files: [file] },
    });
    signInAs("user-2");
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Pull request description" })).toBeNull(),
    );
    await act(async () =>
      upload.resolve(
        json({
          object: "session.github.pr_attachment",
          url: "https://github.com/user-attachments/assets/1",
          name: "shot.png",
          content_type: "image/png",
        }),
      ),
    );
    // User 2 still has no editor; user 1's draft has the upload when they're back.
    expect(screen.queryByRole("textbox", { name: "Pull request description" })).toBeNull();
    signInAs("user-1");
    await waitFor(() =>
      expect(
        (screen.getByRole("textbox", { name: "Pull request description" }) as HTMLTextAreaElement)
          .value,
      ).toContain("https://github.com/user-attachments/assets/1"),
    );
    vi.unstubAllGlobals();
  });
});

describe("the host path", () => {
  it("sends only the field edited, as the host takes it", async () => {
    await installMacWriteShell(PR, async () => ({}));
    // The user keeps every change on the session's machine.
    writeMacWritesIntent("off");
    hostFetch.mockImplementation(async () => json({ title: "New title", body: "Old body" }));
    renderDetails();
    editTitle("New title");
    await waitFor(() => expect(hostFetch).toHaveBeenCalledOnce());
    const [, init] = hostFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ pr_url: PR, title: "New title" });
  });
});

describe("a save that outlives its editor, or must wait", () => {
  it("waits for an upload before Apply mine, as Save does", async () => {
    let calls = 0;
    const { write } = await installMacWriteShell(PR, async () =>
      (calls += 1) === 1 ? changed("body", "Their body") : saved("Old title", "My body"),
    );
    setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
    const upload = deferred<Response>();
    hostFetch.mockImplementation(() => upload.promise);
    renderDetails({ attachments: true });
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Pull request description" }), {
      target: { value: "My body" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    const applyMine = await screen.findByRole("button", { name: "Apply mine" });
    fireEvent.change(screen.getByTestId("pull-request-description-file-input"), {
      target: { files: [new File(["png"], "shot.png", { type: "image/png" })] },
    });
    await waitFor(() => expect(applyMine).toBeDisabled());
    fireEvent.click(applyMine);
    expect(write).toHaveBeenCalledOnce();
  });
});

describe("a host save across a remount", () => {
  function remountableOnHost() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const view = () => (
      <QueryClientProvider client={queryClient}>
        <PullRequestTitle conversationId={SESSION} prUrl={PR} title="Old title" editable />
        <PullRequestDescription
          conversationId={SESSION}
          prUrl={PR}
          body="Old body"
          editable
          attachments={false}
        />
      </QueryClientProvider>
    );
    const first = render(view());
    return { unmount: first.unmount, mountAgain: () => render(view()) };
  }

  it.each([
    ["title", "Pull request title", () => editTitle("Saved title")],
    [
      "description",
      "Pull request description",
      () => {
        fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
        fireEvent.change(screen.getByRole("textbox", { name: "Pull request description" }), {
          target: { value: "Saved body" },
        });
        fireEvent.click(screen.getByRole("button", { name: "Save" }));
      },
    ],
  ] as const)(
    "keeps the %s editor waiting after a remount, and never deletes newer text",
    async (_label, name, startSave) => {
      await installMacWriteShell(PR, async () => ({}));
      const answer = deferred<Response>();
      hostFetch.mockImplementation(() => answer.promise);
      const { unmount, mountAgain } = remountableOnHost();
      startSave();
      unmount();
      mountAgain();
      // Remounted mid-save: still saving, so it can't be edited or saved again.
      const input = screen.getByRole("textbox", { name });
      expect(input).toBeDisabled();
      expect(screen.getByRole("button", { name: /Saving/ })).toBeDisabled();
      // Even text that got in anyway survives the earlier save's answer.
      fireEvent.change(input, { target: { value: "Newer text" } });
      await act(async () => answer.resolve(json({ title: "Saved title", body: "Saved body" })));
      await waitFor(() => expect(screen.getByRole("textbox", { name })).toBeEnabled());
      expect(screen.getByRole("textbox", { name })).toHaveValue("Newer text");
    },
  );
});
