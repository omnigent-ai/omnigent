// The link picker's preview of a typed PR link, looked up only through the session's host.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const host = vi.hoisted(() => ({ serves: true, lookUp: vi.fn() }));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (session: string, method: string) =>
    host.serves && session !== "" && method === "lookupPullRequest",
}));
vi.mock("@/lib/hostPullRequestReads", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestLookupOnHost: host.lookUp,
}));

import { usePullRequestPreview } from "./usePullRequestPreview";

const PR = "https://github.com/acme/app/pull/42";

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

/** A shell that would look the PR up on this device, which the preview never asks. */
function installShell() {
  const codeHostQuery = vi.fn(async () => ({ ok: false, error: { code: "unsupported" } }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: ["pull_request_lookup"], accounts: true }],
    }),
  };
  return codeHostQuery;
}

beforeEach(() => {
  host.serves = true;
  host.lookUp.mockReset();
});
afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("usePullRequestPreview", () => {
  it("previews a typed link through the session's host, even with a shell that reads PRs", async () => {
    const codeHostQuery = installShell();
    host.lookUp.mockResolvedValue({
      url: PR,
      number: 42,
      title: "Faster pull requests",
      state: "MERGED",
      is_draft: false,
      repository: "acme/app",
    });
    const { result } = renderHook(() => usePullRequestPreview(PR, true, "s1"), { wrapper });
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current).toMatchObject({ preview: { title: "Faster pull requests" } });
    expect(host.lookUp).toHaveBeenCalledWith("s1", PR);
    expect(codeHostQuery).not.toHaveBeenCalled();
  });

  it("shows nothing and reads nothing where the host doesn't serve the lookup", async () => {
    host.serves = false;
    installShell();
    const { result } = renderHook(() => usePullRequestPreview(PR, true, "s1"), { wrapper });
    await new Promise((resolve) => {
      setTimeout(resolve, 20);
    });
    expect(result.current).toEqual({ status: "none" });
    expect(host.lookUp).not.toHaveBeenCalled();
  });
});
