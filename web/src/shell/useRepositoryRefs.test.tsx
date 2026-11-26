import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

const fetchRepositoryRefs = vi.hoisted(() => vi.fn());
vi.mock("@/lib/workingCopyComparison", () => ({ fetchRepositoryRefs }));

import { useRepositoryRefs } from "./useRepositoryRefs";

const REPO = { hostId: "host_1", root: "/repo" };
const OTHER = { hostId: "host_1", root: "/other" };

/** One client for the whole test, so switching workspaces shares its cache. */
function withClient() {
  const client = new QueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, wrapper };
}

/** The refs the cache holds for `root`, whichever key holds them. */
const cachedRefsFor = (client: QueryClient, root: string) =>
  client.getQueriesData<{ refs: string[] }>({}).find(([key]) => key.includes(root))?.[1]?.refs;

describe("useRepositoryRefs", () => {
  it("never suggests another workspace's names, and waits for the workspace to be known", async () => {
    fetchRepositoryRefs.mockResolvedValueOnce({
      refs: ["repo-branch"],
      truncated: false,
      defaultBranchRef: "",
    });
    const { client, wrapper } = withClient();
    const { result, rerender } = renderHook(
      ({ workspace }) => useRepositoryRefs("conv_1", workspace, { enabled: true }),
      { wrapper, initialProps: { workspace: null as typeof REPO | null } },
    );
    expect(result.current).toBeUndefined();
    expect(fetchRepositoryRefs).not.toHaveBeenCalled();

    rerender({ workspace: REPO });
    await waitFor(() => expect(result.current?.refs).toEqual(["repo-branch"]));

    fetchRepositoryRefs.mockReturnValueOnce(new Promise(() => {}));
    rerender({ workspace: OTHER });
    // The other repository's names aren't listed yet; the first one's are not shown for it.
    expect(result.current).toBeUndefined();
    expect(fetchRepositoryRefs).toHaveBeenCalledTimes(2);
    // The first workspace's names stay cached under its own key while the other's load.
    expect(cachedRefsFor(client, "/repo")).toEqual(["repo-branch"]);
    expect(cachedRefsFor(client, "/other")).toBeUndefined();
  });
});
