// The header line on the Pull Requests page reading a PR as one of this Mac's logins: on a shell
// that can't make changes as it, one sentence asks for an update; where it can, nothing is said,
// since every change is made as the login shown.

import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
  subscribeIdentity: () => () => {},
}));

import { MacReadAsContext } from "@/hooks/macReadAs";
import {
  resetDesktopPullRequestWritesForTests,
  setMacWritesReleaseFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";

import { ReadSourceStatus } from "./ReadSourceStatus";

const PR = "https://github.com/acme/app/pull/42";
const UPDATE = "Update the app to comment, review and merge as @bob here.";
const PAGE_AS_BOB = { reader: BOB };

function renderOnPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <MacReadAsContext.Provider value={PAGE_AS_BOB}>{children}</MacReadAsContext.Provider>
    </QueryClientProvider>
  );
  render(<ReadSourceStatus prUrl={PR} hostCanAnswer />, { wrapper });
}

afterEach(() => {
  // Unmount first: a mounted header re-reads the stores as they reset, against the old shell.
  cleanup();
  setMacWritesReleaseFlagForTests(null);
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("the header on the Pull Requests page reading as its own login", () => {
  it("says nothing where changes are made as that login", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    renderOnPage();
    expect(screen.queryByText(UPDATE)).toBeNull();
    expect(screen.queryByText(/are made as @/)).toBeNull();
  });

  it("asks for an update, in one sentence, on a shell that can't make them as it", async () => {
    const shell = installNamedWriteShell(undefined, { writeAs: false });
    await shell.ready(PR);
    renderOnPage();
    expect(await screen.findByText(UPDATE)).toBeVisible();
    expect(screen.queryByText(/are made as @/)).toBeNull();
  });

  it("asks once the shell stops offering them, and stops once it does again", async () => {
    const shell = installNamedWriteShell();
    await shell.ready(PR);
    renderOnPage();
    await act(() => shell.setWriteAs(false));
    expect(await screen.findByText(UPDATE)).toBeVisible();
    await act(() => shell.setWriteAs(true));
    expect(screen.queryByText(UPDATE)).toBeNull();
  });
});
