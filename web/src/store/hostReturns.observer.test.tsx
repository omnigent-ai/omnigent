// A host coming back reads its session's pull requests again, on both PR pages: the new page's
// `github-info` family and the classic page's own.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { pullRequestInfoQueryKey } from "@/hooks/usePullRequests";
import { resetHostReturns, useHostReturnObserver } from "@/store/hostReturns";

afterEach(() => resetHostReturns());

describe("useHostReturnObserver", () => {
  it("reads both PR pages' info again once the session's host is back", () => {
    const queryClient = new QueryClient();
    const classic = pullRequestInfoQueryKey("session-1");
    const page = ["pr-page-github-info", "session-1"];
    queryClient.setQueryData(classic, { prs: [] });
    queryClient.setQueryData(page, { prs: [] });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    );
    const view = renderHook(
      ({ online }: { online: boolean }) => useHostReturnObserver(new Map([["session-1", online]])),
      { wrapper, initialProps: { online: false } },
    );

    view.rerender({ online: true });

    expect(queryClient.getQueryState(classic)?.isInvalidated).toBe(true);
    expect(queryClient.getQueryState(page)?.isInvalidated).toBe(true);
  });
});
