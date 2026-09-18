import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { authenticatedFetch } from "@/lib/identity";
import { RecentWorkspaceList } from "./RecentWorkspaceList";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const authenticatedFetchMock = vi.mocked(authenticatedFetch);

function response(remoteProvider?: "github" | "other" | null): Response {
  return new Response(
    JSON.stringify({
      object: "list",
      data: [
        {
          path: "/repo",
          branch: "main",
          is_main: true,
          detached: false,
          ...(remoteProvider === undefined ? {} : { remote_provider: remoteProvider }),
        },
      ],
    }),
    { status: 200, headers: { "content-type": "application/json" } },
  );
}

function renderList(
  props: Partial<React.ComponentProps<typeof RecentWorkspaceList>> = {},
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const onSelect = props.onSelect ?? vi.fn();
  const onBrowse = props.onBrowse ?? vi.fn();
  const view = render(
    <QueryClientProvider client={client}>
      <RecentWorkspaceList
        hostId="host_1"
        paths={["/one", "/two"]}
        onSelect={onSelect}
        onBrowse={onBrowse}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { ...view, onSelect, onBrowse, client };
}

describe("RecentWorkspaceList", () => {
  beforeEach(() => {
    authenticatedFetchMock.mockReset();
    authenticatedFetchMock.mockResolvedValue(response("other"));
  });

  afterEach(() => {
    cleanup();
  });

  it("keeps zero inter-row gap and preserves each row's hit height", () => {
    renderList();
    expect(screen.getByTestId("recent-workspace-list")).toHaveClass("gap-0");
    expect(screen.getByTestId("recent-workspace-select-0")).toHaveClass("py-1.5");
  });

  it("keeps select and browse as sibling actions without cross-triggering", () => {
    const { onSelect, onBrowse } = renderList();
    const row = screen.getByTestId("recent-workspace-row-0");
    expect(row.querySelectorAll(":scope > button")).toHaveLength(2);

    fireEvent.click(screen.getByTestId("recent-workspace-select-0"));
    expect(onSelect).toHaveBeenCalledWith("/one");
    expect(onBrowse).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("recent-workspace-browse-0"));
    expect(onBrowse).toHaveBeenCalledWith("/one");
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("uses GitHub icons only for positively verified GitHub providers", async () => {
    authenticatedFetchMock
      .mockResolvedValueOnce(response("github"))
      .mockResolvedValueOnce(response("other"));
    renderList();

    expect(await screen.findByTestId("recent-workspace-icon-0-github")).toBeInTheDocument();
    expect(await screen.findByTestId("recent-workspace-icon-1-folder")).toBeInTheDocument();
  });

  it("falls back to folder icons for legacy, offline, and unavailable metadata", async () => {
    authenticatedFetchMock
      .mockResolvedValueOnce(response())
      .mockRejectedValueOnce(new Error("host offline"));
    renderList();

    await waitFor(() => {
      expect(screen.getByTestId("recent-workspace-icon-0-folder")).toBeInTheDocument();
      expect(screen.getByTestId("recent-workspace-icon-1-folder")).toBeInTheDocument();
    });
  });

  it("does not reuse a previous path's GitHub icon when a row path changes", async () => {
    let resolveNext: ((value: Response) => void) | undefined;
    authenticatedFetchMock.mockResolvedValueOnce(response("github")).mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          resolveNext = resolve;
        }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { rerender } = renderList({ paths: ["/github"] }, client);
    expect(await screen.findByTestId("recent-workspace-icon-0-github")).toBeInTheDocument();

    rerender(
      <QueryClientProvider client={client}>
        <RecentWorkspaceList
          hostId="host_1"
          paths={["/ordinary"]}
          onSelect={vi.fn()}
          onBrowse={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("recent-workspace-icon-0-folder")).toBeInTheDocument();
    expect(screen.queryByTestId("recent-workspace-icon-0-github")).not.toBeInTheDocument();

    resolveNext?.(response("other"));
    await waitFor(() =>
      expect(authenticatedFetchMock).toHaveBeenLastCalledWith(
        "/v1/hosts/host_1/worktrees?path=%2Fordinary",
      ),
    );
  });
});
