import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useGitHubFastTrack } from "./useGitHubFastTrack";

const ACCOUNT = { host: "github.com", login: "octo_data" };

function render(account: typeof ACCOUNT | null) {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  return renderHook(() => useGitHubFastTrack(account), { wrapper });
}

function installShell(bridge: Record<string, unknown> | undefined) {
  const target = window as unknown as Record<string, unknown>;
  if (bridge) target.omnigentDesktop = { kind: "electron", ...bridge };
  else delete target.omnigentDesktop;
}

afterEach(() => installShell(undefined));

describe("useGitHubFastTrack", () => {
  it("is unsupported at once outside the shell or without an account", () => {
    expect(render(ACCOUNT).result.current).toEqual({ status: "unsupported" });
    const githubQuery = vi.fn();
    installShell({ githubQuery, getDesktopFeatures: async () => ({ githubFastTrack: true }) });
    expect(render(null).result.current).toEqual({ status: "unsupported" });
    expect(githubQuery).not.toHaveBeenCalled();
  });

  it("checks the account, then reports it ready", async () => {
    installShell({
      githubQuery: vi.fn().mockResolvedValue({ ok: true, data: { login: "octo_data" } }),
      getDesktopFeatures: async () => ({ githubFastTrack: true }),
    });
    const { result } = render(ACCOUNT);
    expect(result.current).toEqual({ status: "checking" });
    await waitFor(() => expect(result.current).toEqual({ status: "ready", account: ACCOUNT }));
  });

  it("settles unsupported in an older shell that reports no feature", async () => {
    installShell({ githubQuery: vi.fn(), getDesktopFeatures: async () => ({}) });
    const { result } = render(ACCOUNT);
    await waitFor(() => expect(result.current).toEqual({ status: "unsupported" }));
  });
});
