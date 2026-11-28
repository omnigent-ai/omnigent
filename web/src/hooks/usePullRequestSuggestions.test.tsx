import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { usePullRequestSuggestions } from "./usePullRequestSuggestions";

function installShell(operations: string[]) {
  const githubQuery = vi.fn(async () => ({
    ok: false,
    error: { code: "signed_out", message: "The host's account isn't signed in on this Mac." },
  }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    githubQuery,
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: operations,
    }),
  };
  return { githubQuery };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("usePullRequestSuggestions", () => {
  it("reads suggestions with the fast track as the host's account", async () => {
    const { githubQuery } = installShell(["account_status", "pull_request_suggestions"]);
    const { result } = renderHook(
      () => usePullRequestSuggestions({ host: "github.com", login: "host-user" }, null, true),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(githubQuery).toHaveBeenCalled();
  });

  it("keeps paste-only linking where the shell can't suggest", async () => {
    const { githubQuery } = installShell(["account_status"]);
    const { result } = renderHook(
      () => usePullRequestSuggestions({ host: "github.com", login: "host-user" }, null, true),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("unsupported"));
    expect(githubQuery).not.toHaveBeenCalled();
  });
});
