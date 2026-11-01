import type * as IdentityModule from "@/lib/identity";
import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";

import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FileContentResponse } from "@/hooks/useFileContent";
import { FileViewerContext } from "@/shell/FileViewerContext";
import { ToolResultImages } from "./ToolResultImages";

// Only the HTTP boundary and the runner/host liveness gate are faked; path
// resolution, the filesystem query and the base64 decode stay real.
vi.mock("@/lib/identity", async (importOriginal) => {
  const actual = await importOriginal<typeof IdentityModule>();
  return { ...actual, authenticatedFetch: vi.fn() };
});
const serveable = vi.hoisted(() => ({ value: true as boolean | undefined }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof WorkspaceChangedFilesModule>();
  return { ...actual, useWorkspaceServeable: () => serveable.value };
});
import { authenticatedFetch } from "@/lib/identity";

function respondWith(body: FileContentResponse) {
  vi.mocked(authenticatedFetch).mockImplementation(async () => Response.json(body));
}

function requestedUrls(): string[] {
  return vi.mocked(authenticatedFetch).mock.calls.map(([input]) => String(input));
}

const PNG_RESPONSE: FileContentResponse = {
  object: "session.environment.filesystem.file_content",
  path: "shots/home.png",
  content_type: "image/png",
  encoding: "base64",
  content: btoa("\x89PNG-bytes"),
  bytes: 9,
};

const createObjectURL = vi.fn((_blob: Blob | MediaSource) => "blob:mock-1");
const revokeObjectURL = vi.fn();
const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeEach(() => {
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
  serveable.value = true;
  respondWith(PNG_RESPONSE);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  URL.createObjectURL = originalCreateObjectURL;
  URL.revokeObjectURL = originalRevokeObjectURL;
});

type Viewer = Partial<{
  conversationId: string;
  workspaceRoot: string | null;
  workspaceHome: string | null;
  workspacePathsLoading: boolean;
}>;

function renderPreview(path: string, viewer: Viewer = {}) {
  // Default retries stay on so a test sees whether the preview opts out of them.
  const queryClient = new QueryClient();
  const tree = (current: Viewer) => {
    const context = {
      openFile: () => {},
      openGithubTab: () => {},
      isChangedPath: () => false,
      conversationId: "conv-1",
      workspaceRoot: "/ws",
      workspaceHome: "/home/agent",
      ...current,
    };
    const preview = createElement(
      FileViewerContext.Provider,
      { value: context },
      <ToolResultImages images={[{ kind: "file", path, alt: "home.png" }]} />,
    );
    return <QueryClientProvider client={queryClient}>{preview}</QueryClientProvider>;
  };
  const view = render(tree(viewer));
  return { queryClient, setViewer: (next: Viewer) => view.rerender(tree(next)) };
}

describe("ToolResultImages", () => {
  it("renders inline images as zoomable previews without a fetch", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    render(<ToolResultImages images={[{ kind: "inline", src, alt: "Tool result image" }]} />);

    expect(screen.getByRole("img", { name: "Tool result image" })).toHaveAttribute("src", src);
    expect(screen.getByRole("button", { name: "Zoom image: Tool result image" })).toBeVisible();
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("reads a workspace image by its workspace-relative path and shows its bytes", async () => {
    renderPreview("/ws/shots/home.png");

    expect(screen.getByRole("status", { name: "Loading image" })).toBeVisible();
    const image = await screen.findByRole("img", { name: "home.png" });
    expect(image).toHaveAttribute("src", "blob:mock-1");
    // The capped JSON read, not a download: it never wakes a sleeping runner.
    expect(requestedUrls()).toEqual([
      "/v1/sessions/conv-1/resources/environments/default/filesystem/shots/home.png",
    ]);
    expect(createObjectURL.mock.calls[0]![0]).toMatchObject({ type: "image/png" });
  });

  it.each([
    ["an absolute path", "/ws/shots/home.png", { workspaceRoot: null }],
    ["a home-relative path", "~/shots/home.png", { workspaceHome: null }],
  ])("waits for the workspace environment before reading %s", async (_label, path, paths) => {
    const { setViewer } = renderPreview(path, { ...paths, workspacePathsLoading: true });

    expect(screen.getByRole("status", { name: "Loading image" })).toBeVisible();
    expect(authenticatedFetch).not.toHaveBeenCalled();

    setViewer({ workspaceRoot: "/ws", workspaceHome: "/ws" });
    expect(await screen.findByRole("img", { name: "home.png" })).toHaveAttribute(
      "src",
      "blob:mock-1",
    );
    expect(requestedUrls()).toEqual([
      "/v1/sessions/conv-1/resources/environments/default/filesystem/shots/home.png",
    ]);
  });

  it("shows the chip for a missing file without retrying the read", async () => {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response("missing", { status: 404 }),
    );
    renderPreview("a.png");

    const chip = await screen.findByRole("img", { name: "home.png" });
    expect(chip).not.toHaveAttribute("src");
    expect(requestedUrls()).toHaveLength(1);
  });

  it("drops a shown image for the chip once a refresh finds the file gone", async () => {
    const { queryClient } = renderPreview("shots/home.png");
    expect(await screen.findByRole("img", { name: "home.png" })).toHaveAttribute(
      "src",
      "blob:mock-1",
    );

    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response("missing", { status: 404 }),
    );
    await act(() => queryClient.invalidateQueries());

    await waitFor(() =>
      expect(screen.getByRole("img", { name: "home.png" })).not.toHaveAttribute("src"),
    );
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-1");
  });

  it.each<[string, () => void, string, Viewer?]>([
    [
      "the file is too large to return",
      () => respondWith({ ...PNG_RESPONSE, truncated: true }),
      "a.png",
    ],
    ["neither runner nor host can serve it", () => (serveable.value = false), "a.png"],
    ["the path is unsafe", () => {}, "../outside.png"],
    ["no session is focused", () => {}, "a.png", { conversationId: undefined }],
    [
      "the workspace root can't be discovered",
      () => {},
      "/ws/shots/home.png",
      { workspaceRoot: null, workspacePathsLoading: false },
    ],
    [
      "the runner home can't be discovered",
      () => {},
      "~/shots/home.png",
      { workspaceHome: null, workspacePathsLoading: false },
    ],
  ])("shows the unavailable chip when %s", async (_label, arrange, path, viewer) => {
    arrange();
    renderPreview(path, viewer);

    await waitFor(() => {
      const chip = screen.getByRole("img", { name: "home.png" });
      expect(chip).not.toHaveAttribute("src");
      expect(chip).toHaveTextContent("home.png");
    });
  });
});
