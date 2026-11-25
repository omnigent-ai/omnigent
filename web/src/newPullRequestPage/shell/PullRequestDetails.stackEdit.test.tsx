// Editing a stacked PR's description on the new page, over the Mac write path: only the text
// after a verified stack section, the section and the text above it kept as GitHub has them,
// and a stack tool's rewrite of that section moved under the edit instead of lost or overwritten.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { hostFetch } = vi.hoisted(() => ({ hostFetch: vi.fn() }));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
  authenticatedFetch: hostFetch,
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import {
  GITHUB_WRITE_SET_V1,
  resetDesktopPullRequestWritesForTests,
  setPageWriteOperationsForTests,
  writeMacWritesIntent,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { EXPECTED_CHECK_UNAVAILABLE } from "@/newPullRequestPage/hooks/usePullRequests";
import { EXAMPLE_STACK_FORMAT, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";
import { STACK_INFO_UPDATED } from "@/lib/pullRequestStacks/stackDescriptionDraft";
import { parseStack } from "@/lib/pullRequestStacks/stackFormats";
import { PullRequestImageScope } from "@/pullRequestPage/PullRequestPageImage";
import { pullRequestStackView } from "@/pullRequestPage/usePullRequestStack";
import { installMacWriteShell, removeMacWriteShell } from "@/test/macWriteShell";

import { PullRequestDescription, resetPullRequestDescriptionDrafts } from "./PullRequestDetails";

// Stack descriptions in an example format, which the page reads once a build registers it.
const STACK_FORMATS = [EXAMPLE_STACK_FORMAT];
withPullRequestIntegrations({ stackFormats: STACK_FORMATS });

vi.mock("@/lib/pullRequestRpc", async (importOriginal) =>
  (await import("@/test/pullRequestRpcOverRest")).pullRequestRpcOverRest(await importOriginal()),
);

const PR = "https://github.com/acme/app/pull/101";
const url = (number: number) => `https://github.com/acme/app/pull/${number}`;

function section(children: number[] = [], footer = "---------", base = 100): string {
  return [
    "## Stacked changes",
    `- [stack/${base}](${url(base)})`,
    `  - [**stack/101**](${url(101)}) ← _this PR_`,
    ...children.map((number) => `    - [stack/${number}](${url(number)})`),
    "",
    footer,
    "",
  ].join("\n");
}

const ORIGINAL = `${section()}The change.\n`;

const saved = (body: string) => ({
  ok: true,
  data: { object: "session.github.pr_details", title: "Title", body },
  actor: { login: "mac-octo" },
});
const changed = (value: string) => ({
  ok: false,
  error: {
    code: "pull_request_changed",
    message: "This description changed on GitHub since you started editing.",
  },
  current: { field: "body", value },
});

/** The shell answering each write with the next of `answers`. */
async function shellAnswering(...answers: unknown[]) {
  const queue = [...answers];
  const shell = await installMacWriteShell(PR, async () => queue.shift() ?? saved(ORIGINAL));
  setPageWriteOperationsForTests(GITHUB_WRITE_SET_V1);
  return shell;
}

const paramsOf = (write: ReturnType<typeof vi.fn>, call: number) =>
  (write.mock.calls[call][0] as { params: { value: string; expected: string } }).params;

function stackOf(body: string) {
  const parse = parseStack(STACK_FORMATS, body, {
    host: "github.com",
    repository: "acme/app",
    number: 101,
  });
  return parse && pullRequestStackView(parse, { kind: "unavailable" }, () => true);
}

/** The description as the new page shows it, or as the classic panel does with `classic`. */
function description(body: string, { classic = false } = {}) {
  const view = (
    <PullRequestDescription
      conversationId="session-1"
      prUrl={PR}
      body={body}
      stack={classic ? null : stackOf(body)}
      editable
      attachments={false}
    />
  );
  return classic ? view : <PullRequestImageScope prUrl={PR}>{view}</PullRequestImageScope>;
}

function renderDescription(body = ORIGINAL, options: { classic?: boolean } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrap = (children: React.ReactNode) => (
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <TooltipProvider>{children}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  const view = render(wrap(description(body, options)));
  return {
    ...view,
    /** The page reading `next` from GitHub, with the same editor and draft open. */
    show: (next: string, nextOptions: { classic?: boolean } = {}) =>
      view.rerender(wrap(description(next, nextOptions))),
  };
}

/** The other PRs the read-only stack above the editor links to; this PR's row isn't a link. */
function keptStackNumbers(): number[] {
  const stack = screen.getByRole("navigation", { name: "Stack" });
  return Array.from(stack.querySelectorAll("a[href]"), (link) =>
    Number(link.getAttribute("href")?.split("/").pop()),
  );
}

const composer = () =>
  screen.getByRole("textbox", { name: "Pull request description" }) as HTMLTextAreaElement;

function edit(value: string) {
  fireEvent.change(composer(), { target: { value } });
}

/** ⌘↵ in the composer; jsdom isn't a Mac, so the chord is Control+Enter. */
function pressSave() {
  act(() => {
    composer().dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        ctrlKey: true,
        bubbles: true,
        cancelable: true,
      }),
    );
  });
}

beforeEach(() => {
  hostFetch.mockReset();
  resetDesktopPullRequestWritesForTests();
  resetPullRequestDescriptionDrafts();
});

afterEach(() => {
  writeMacWritesIntent("automatic");
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetPullRequestDescriptionDrafts();
  removeMacWriteShell();
});

describe("editing a stacked PR's description", () => {
  it("edits only the text after the stack section and saves it after the section as it was", async () => {
    const above = "Read this first.  \n\n";
    const original = `${above}${ORIGINAL}`;
    const { write } = await shellAnswering(saved(`${above}${section()}My edit.\n`));
    renderDescription(original);
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    expect(composer().value).toBe("The change.\n");
    // The text above and the stack show as they are, outside the editor.
    expect(screen.getByText("Text above the stack info")).toBeInTheDocument();
    expect(screen.getByText("Read this first.")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Stack" })).toBeInTheDocument();
    edit("My edit.\n");
    pressSave();
    expect(paramsOf(write, 0)).toEqual(
      expect.objectContaining({
        value: `${above}${section()}My edit.\n`,
        expected: original,
      }),
    );
    await waitFor(() => expect(screen.queryByRole("textbox")).toBeNull());
  });

  it("splices nothing once the person edits the full description", async () => {
    const { write } = await shellAnswering(saved("Rewritten.\n"));
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    fireEvent.click(screen.getByRole("button", { name: "Edit full description" }));
    expect(composer().value).toBe(ORIGINAL);
    expect(screen.queryByRole("navigation", { name: "Stack" })).toBeNull();
    edit("Rewritten.\n");
    pressSave();
    expect(paramsOf(write, 0)).toEqual(
      expect.objectContaining({ value: "Rewritten.\n", expected: ORIGINAL }),
    );
  });

  it("edits a stack section that isn't in the usual format as part of the whole description", async () => {
    await shellAnswering();
    const tolerated = `${section([], "---")}The change.\n`;
    renderDescription(tolerated);
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    expect(composer().value).toBe(tolerated);
    expect(screen.queryByRole("button", { name: "Edit full description" })).toBeNull();
  });
});

describe("a stacked PR's description that changed on GitHub while being edited", () => {
  it("keeps the edit over a stack tool's new section, unsaved, for the person to review", async () => {
    const pushed = `${section([102])}The change.\n`;
    const { write } = await shellAnswering(changed(pushed), saved(`${section([102])}My edit.\n`));
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    edit("My edit.\n");
    pressSave();
    expect(await screen.findByText(STACK_INFO_UPDATED)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(composer().value).toBe("My edit.\n");
    // Nothing more is sent until the person saves again, over the section GitHub has now.
    expect(write).toHaveBeenCalledTimes(1);
    pressSave();
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(paramsOf(write, 1)).toEqual(
      expect.objectContaining({ value: `${section([102])}My edit.\n`, expected: pushed }),
    );
  });

  it("asks when someone changed the text too, and Apply mine keeps GitHub's newer section", async () => {
    const theirs = `${section([102])}Their edit.\n`;
    const { write } = await shellAnswering(changed(theirs), saved(`${section([102])}My edit.\n`));
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    edit("My edit.\n");
    pressSave();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "This description changed on GitHub since you started editing.",
    );
    expect(composer().value).toBe("My edit.\n");
    fireEvent.click(screen.getByRole("button", { name: "Apply mine" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    // The stale section is never written back over the newer one.
    expect(paramsOf(write, 1)).toEqual(
      expect.objectContaining({ value: `${section([102])}My edit.\n`, expected: theirs }),
    );
  });

  it("hands back the whole description when GitHub's section isn't in the usual format any more", async () => {
    const loose = `${section([], "---")}Their edit.\n`;
    const { write } = await shellAnswering(changed(loose), saved(`${section()}My edit.\n`));
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    edit("My edit.\n");
    pressSave();
    await screen.findByRole("alert");
    // The person now sees exactly what Apply mine would write, stack section included.
    expect(composer().value).toBe(`${section()}My edit.\n`);
    expect(screen.queryByRole("button", { name: "Edit full description" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Apply mine" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(paramsOf(write, 1)).toEqual(
      expect.objectContaining({ value: `${section()}My edit.\n`, expected: loose }),
    );
  });
});

describe("where a save can't check GitHub's text first", () => {
  it("edits the whole description when changes go through the session's machine", async () => {
    await shellAnswering();
    // The user keeps every change on the session's machine, which doesn't check `expected`.
    writeMacWritesIntent("off");
    hostFetch.mockImplementation(
      async () => new Response(JSON.stringify({ title: "Title", body: "Rewritten.\n" })),
    );
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    expect(composer().value).toBe(ORIGINAL);
    expect(screen.queryByRole("button", { name: "Edit full description" })).toBeNull();
    edit("Rewritten.\n");
    pressSave();
    await waitFor(() => expect(hostFetch).toHaveBeenCalledOnce());
  });

  it("refuses, keeping the edit, once changes stop going through this Mac mid-edit", async () => {
    const { write } = await shellAnswering();
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    edit("My edit.\n");
    writeMacWritesIntent("off");
    pressSave();
    expect(await screen.findByRole("alert")).toHaveTextContent(EXPECTED_CHECK_UNAVAILABLE);
    expect(composer().value).toBe("My edit.\n");
    expect(write).not.toHaveBeenCalled();
    expect(hostFetch).not.toHaveBeenCalled();
    // Editing the whole description is the person's explicit way to save it there.
    fireEvent.click(screen.getByRole("button", { name: "Edit full description" }));
    expect(composer().value).toBe(`${section()}My edit.\n`);
  });
});

describe("the stack shown above the editor", () => {
  it("is the section a rebased draft will save, though the page still shows the old one", async () => {
    // A stack tool moved this PR onto #103 and added #102 after it.
    const pushed = `${section([102], "---------", 103)}The change.\n`;
    await shellAnswering(changed(pushed));
    renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    expect(keptStackNumbers()).toEqual([100]);
    edit("My edit.\n");
    pressSave();
    expect(await screen.findByText(STACK_INFO_UPDATED)).toBeInTheDocument();
    expect(keptStackNumbers()).toEqual([103, 102]);
  });

  it("stays the draft's own section when the page reads a newer one", async () => {
    await shellAnswering();
    const { show } = renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    show(`${section([102])}The change.\n`);
    expect(keptStackNumbers()).toEqual([100]);
  });
});

describe("a draft opened around a stack section, then edited in the classic panel", () => {
  it("is saved as the whole description it became, never cut at the old section", async () => {
    const pushed = `${section([102])}The change.\n`;
    const { write } = await shellAnswering(changed(pushed), saved("Rewritten whole.\n"));
    const { show } = renderDescription();
    fireEvent.click(screen.getByRole("button", { name: "Edit description" }));
    // The new page is turned off: the same draft opens in the classic textarea.
    show(ORIGINAL, { classic: true });
    expect(composer().value).toBe(ORIGINAL);
    edit("Rewritten whole.\n");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    // GitHub's change was only to the stack, but this draft rewrote that too: the person decides.
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(STACK_INFO_UPDATED)).toBeNull();
    expect(composer().value).toBe("Rewritten whole.\n");
    fireEvent.click(screen.getByRole("button", { name: "Apply mine" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(2));
    expect(paramsOf(write, 1)).toEqual(
      expect.objectContaining({ value: "Rewritten whole.\n", expected: pushed }),
    );
  });
});
