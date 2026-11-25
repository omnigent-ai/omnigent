import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const offline = vi.hoisted(() => new Set<string>());
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useWorkspaceServeable: (conversationId: string) => !offline.has(conversationId),
}));

import {
  chooseReadSource,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  readChoiceFor,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";

import { PullRequestPageScope } from "./PullRequestPageScope";

const PR = "https://github.com/acme/app/pull/1";

function installCapableShell() {
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(async () => ({
      ok: true,
      data: [{ login: "octo", active: true, state: "success" }],
    })),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [{ id: "github", operations: [...GITHUB_MAC_READ_OPERATIONS], accounts: true }],
    }),
  };
}

function scope(conversationId: string, body: string) {
  return (
    <section aria-label={conversationId}>
      <PullRequestPageScope enabled conversationId={conversationId} prUrl={PR} active>
        <p>{body}</p>
      </PullRequestPageScope>
    </section>
  );
}

afterEach(() => {
  offline.clear();
  resetDesktopReadPathForTests();
  githubMacReadsPreference.write("automatic");
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
});

describe("PullRequestPageScope", () => {
  it("offers this Mac where the chosen session machine is away, whatever the details show", async () => {
    installCapableShell();
    githubMacReadsPreference.write("on");
    await act(async () => {
      await desktopReadTarget(PR);
    });
    act(() => chooseReadSource(PR, "session"));
    // Two sessions show the PR; only the first one's machine is offline.
    offline.add("conv_a");
    render(
      <QueryClientProvider client={new QueryClient()}>
        {scope("conv_a", "The PR as the server stored it")}
        {scope("conv_b", "The PR from its machine")}
      </QueryClientProvider>,
    );
    const away = within(screen.getByRole("region", { name: "conv_a" }));
    expect(away.getByText("The session’s machine can’t be reached.")).toBeVisible();
    expect(away.getByText("The PR as the server stored it")).toBeVisible();
    const here = within(screen.getByRole("region", { name: "conv_b" }));
    expect(here.queryByText("The session’s machine can’t be reached.")).toBeNull();

    fireEvent.click(away.getByRole("button", { name: "Use this Mac" }));
    expect(readChoiceFor(PR)).toBe("automatic");
  });

  it("stays quiet on the classic page", async () => {
    installCapableShell();
    githubMacReadsPreference.write("on");
    await act(async () => {
      await desktopReadTarget(PR);
    });
    act(() => chooseReadSource(PR, "session"));
    offline.add("conv_a");
    render(
      <QueryClientProvider client={new QueryClient()}>
        <PullRequestPageScope enabled={false} conversationId="conv_a" prUrl={PR} active>
          <p>Classic</p>
        </PullRequestPageScope>
      </QueryClientProvider>,
    );
    expect(screen.queryByText("The session’s machine can’t be reached.")).toBeNull();
  });
});
