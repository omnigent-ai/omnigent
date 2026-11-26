// useSetPullRequestPreference against a real query cache, with fetch stubbed.

import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { useSetPullRequestPreference, type PullRequestInfo } from "./usePullRequests";

const PR_A = "https://github.com/acme/app/pull/1";
const PR_B = "https://github.com/acme/app/pull/2";

afterEach(() => vi.unstubAllGlobals());

function info(selectedPrUrl: string, shape: "legacy" | "auth"): PullRequestInfo {
  const accounts = [
    { login: "work", active: true, state: "success", host: "github.com" },
    { login: "personal", active: false, state: "success", host: "github.com" },
  ];
  return {
    object: "session.github.info",
    available: true,
    selected_pr_url: selectedPrUrl,
    ...(shape === "legacy"
      ? { accounts, selected_account: "work" }
      : {
          auth: {
            authenticated: true,
            hint: null,
            cli: null,
            accounts,
            selected_account: "work",
          },
        }),
  } as PullRequestInfo;
}

it("forgets only that PR's account when a switch's reply fails", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify({ detail: "Host offline" }), { status: 400 })),
  );
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const cached = {
    // An older host's payload carries only the legacy top-level fields.
    prA: [["github-info", "conv_1", PR_A], info(PR_A, "legacy")],
    session: [["github-info", "conv_1"], info(PR_A, "auth")],
    prB: [["github-info", "conv_1", PR_B], info(PR_B, "auth")],
    otherConversation: [["github-info", "conv_2", PR_A], info(PR_A, "auth")],
  } as const;
  for (const [key, data] of Object.values(cached)) client.setQueryData(key, data);
  const { result } = renderHook(() => useSetPullRequestPreference("conv_1"), {
    wrapper: ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    ),
  });

  act(() => result.current.mutate({ account: "personal", pr_url: PR_A }));
  await waitFor(() => expect(result.current.isError).toBe(true));

  const read = (key: readonly unknown[]) => client.getQueryData<PullRequestInfo>(key);
  // The host may have saved `personal`, so nothing may still claim `work` for PR A.
  expect(read(cached.prA[0])).toMatchObject({ selected_account: null });
  expect(read(cached.prA[0])?.auth).toBeUndefined();
  expect(read(cached.session[0])?.auth?.selected_account).toBeNull();
  // Another PR, and another conversation, keep theirs.
  expect(read(cached.prB[0])).toEqual(cached.prB[1]);
  expect(read(cached.otherConversation[0])).toEqual(cached.otherConversation[1]);
});
