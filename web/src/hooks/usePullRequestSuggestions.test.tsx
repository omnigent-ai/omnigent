import type * as IdentityModule from "@/lib/identity";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => "user-1",
}));

import {
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  PANEL_READ_OPERATIONS,
} from "@/lib/desktopReadPath";
import { usePullRequestSuggestions } from "./usePullRequestSuggestions";
import { EXAMPLE_SPOKE_HOST, withPullRequestIntegrations } from "@/test/pullRequestIntegrations";

// GitSpoke stand-in hosts are recognized by name, as a build that registers GitSpoke does.
withPullRequestIntegrations({ namedCodeHosts: [EXAMPLE_SPOKE_HOST] });

const SPOKE = "2.spoke.example.com";
const SUGGESTIONS = { repository: [], mine: [], recent: [] };

function installShell() {
  const codeHostQuery = vi.fn(async (request: { operation: string; host: string }) => ({
    ok: true,
    data: SUGGESTIONS,
    ...(request.host === SPOKE
      ? {}
      : { identity: { host: request.host, login: "mac-me", generation: 1 } }),
  }));
  const githubQuery = vi.fn(async () => ({
    ok: false,
    error: { code: "signed_out", message: "The host's account isn't signed in on this Mac." },
  }));
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery,
    githubQuery,
    codeHostAccounts: { get: vi.fn(), set: vi.fn(), onChanged: () => () => {} },
    getDesktopFeatures: async () => ({
      githubFastTrack: true,
      githubFastTrackOperations: ["account_status", "pull_request_suggestions"],
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_suggestions"],
          accounts: true,
        },
        { id: "gitspoke", operations: [...PANEL_READ_OPERATIONS, "pull_request_suggestions"] },
      ],
    }),
  };
  return { codeHostQuery, githubQuery };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("usePullRequestSuggestions on this Mac", () => {
  it("suggests GitSpoke PRs from this Mac's certificate, not the host's account", async () => {
    const { codeHostQuery, githubQuery } = installShell();
    const { result } = renderHook(
      () =>
        usePullRequestSuggestions(null, "example-org/universe", true, {
          provider: "gitspoke",
          host: SPOKE,
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(codeHostQuery).toHaveBeenCalledWith(
      expect.objectContaining({
        provider: "gitspoke",
        host: SPOKE,
        operation: "pull_request_suggestions",
        params: { repository: "example-org/universe" },
      }),
    );
    expect(codeHostQuery.mock.calls[0][0]).not.toHaveProperty("account");
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it.each([
    ["no host account", null],
    ["a different host account", { host: "github.com", login: "host-user" }],
  ])("suggests github.com PRs as this Mac's account with %s", async (_name, hostAccount) => {
    const { codeHostQuery, githubQuery } = installShell();
    githubMacReadsPreference.write("on");
    const { result } = renderHook(
      () =>
        usePullRequestSuggestions(hostAccount, "example-org/universe", true, {
          provider: "github",
          host: "github.com",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(codeHostQuery).toHaveBeenCalledWith(
      expect.objectContaining({ provider: "github", account: "mac" }),
    );
    expect(codeHostQuery.mock.calls[0][0]).not.toHaveProperty("login");
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("keeps the host account's fast track while GitHub reads on this Mac are off", async () => {
    const { codeHostQuery, githubQuery } = installShell();
    githubMacReadsPreference.write("off");
    const { result } = renderHook(
      () =>
        usePullRequestSuggestions({ host: "github.com", login: "host-user" }, null, true, {
          provider: "github",
          host: "github.com",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(githubQuery).toHaveBeenCalled();
    expect(codeHostQuery).not.toHaveBeenCalledWith(
      expect.objectContaining({ operation: "pull_request_suggestions" }),
    );
  });
});
