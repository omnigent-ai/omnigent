import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConversationScopeContext } from "@/components/chat/conversationScope";
import { ImageLightboxProvider } from "@/components/ImageLightbox";
import { FileViewerContext } from "@/shell/FileViewerContext";
import type { FileContentResponse } from "@/hooks/useFileContent";
import type * as workspaceFiles from "@/hooks/useWorkspaceChangedFiles";
import { BlockRenderer } from "./BlockRenderer";
import { FilePathAwareMessageResponse } from "./ChatMarkdown";

const { workspaceEnvironment } = vi.hoisted(() => ({ workspaceEnvironment: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (original) => ({
  ...(await original<typeof workspaceFiles>()),
  useWorkspaceEnvironment: (id: string | undefined) => workspaceEnvironment(id),
}));

const fetchMock = vi.fn();
const createObjectURL = vi.fn();
const revokeObjectURL = vi.fn();
const ORIGINAL_CREATE = URL.createObjectURL;
const ORIGINAL_REVOKE = URL.revokeObjectURL;
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
const MAX_ENCODED_LENGTH = Math.ceil(MAX_IMAGE_BYTES / 3) * 4;
const VIEWER = {
  openFile: vi.fn(),
  openGithubTab: vi.fn(),
  isChangedPath: () => false,
  conversationId: "session-main" as string | undefined,
  workspaceRoot: "/workspace/project" as string | null,
  workspaceHome: "/home/user",
};
const PNG: FileContentResponse = {
  object: "session.environment.filesystem.file_content",
  path: "shot.png",
  content_type: "image/png",
  encoding: "base64",
  content: "iVBORw0KGgo=",
  bytes: 8,
};

function imageResponse(data: Partial<FileContentResponse> = {}, acknowledged = true) {
  return new Response(JSON.stringify({ ...PNG, ...data }), {
    headers: acknowledged ? { "X-Omnigent-File-Scope": "workspace" } : {},
  });
}

beforeEach(() => {
  fetchMock.mockReset().mockImplementation(() => Promise.resolve(imageResponse()));
  createObjectURL
    .mockReset()
    .mockImplementation(() => `blob:workspace-image-${createObjectURL.mock.calls.length}`);
  revokeObjectURL.mockReset();
  workspaceEnvironment.mockReset().mockReturnValue({ data: undefined });
  vi.stubGlobal("fetch", fetchMock);
  URL.createObjectURL = createObjectURL;
  URL.revokeObjectURL = revokeObjectURL;
});

afterEach(() => {
  cleanup();
  URL.createObjectURL = ORIGINAL_CREATE;
  URL.revokeObjectURL = ORIGINAL_REVOKE;
  vi.unstubAllGlobals();
});

function renderMarkdown(
  markdown: string,
  options: {
    viewer?: typeof VIEWER;
    scopedId?: string;
    enabled?: boolean;
    streaming?: boolean;
  } = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  const element = (text: string, viewer = options.viewer ?? VIEWER) => (
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={viewer}>
        <ConversationScopeContext.Provider value={options.scopedId ?? null}>
          <ImageLightboxProvider>
            {options.enabled === false ? (
              <FilePathAwareMessageResponse mode="static">{text}</FilePathAwareMessageResponse>
            ) : (
              <BlockRenderer
                items={[{ kind: "text", itemId: "reply", text, final: !options.streaming }]}
                sessionStatus={options.streaming ? "running" : "idle"}
                isLastAssistant
              />
            )}
          </ImageLightboxProvider>
        </ConversationScopeContext.Provider>
      </FileViewerContext.Provider>
    </QueryClientProvider>
  );
  const result = render(element(markdown));
  return {
    ...result,
    update: (text: string, viewer = VIEWER) => result.rerender(element(text, viewer)),
  };
}

describe("assistant workspace markdown images", () => {
  it.each([false, true])(
    "loads an authenticated blob in streaming=%s replies",
    async (streaming) => {
      const result = renderMarkdown("![Screenshot](/workspace/project/shot.png)", { streaming });

      const zoom = await screen.findByRole("button", { name: "Zoom image: Screenshot" });
      expect(screen.getByRole("img", { name: "Screenshot" })).toHaveAttribute(
        "src",
        "blob:workspace-image-1",
      );
      expect(fetchMock).toHaveBeenCalledWith(
        "/v1/sessions/session-main/resources/environments/default/filesystem/shot.png?scope=workspace",
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(createObjectURL.mock.calls[0][0]).toMatchObject({ size: 8, type: "image/png" });
      fireEvent.click(zoom);
      expect(await screen.findByRole("dialog")).toBeVisible();
      result.unmount();
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:workspace-image-1");
    },
  );

  it("loads a typed WebP blob when MIME metadata is absent", async () => {
    const content =
      "UklGRj4AAABXRUJQVlA4IDIAAADQAQCdASoBAAEAAUAmJaACdLoB+AADsAD+6SIf+8+fufP3" +
      "Pn/Rn/+U/fI4/kcf/KBAAA==";
    fetchMock.mockResolvedValue(
      imageResponse({ path: "shot.webp", content_type: null, content, bytes: 70 }),
    );
    renderMarkdown("![Screenshot](shot.webp)");

    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(screen.getByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:workspace-image-1",
    );
    expect(createObjectURL.mock.calls[0][0]).toMatchObject({ size: 70, type: "image/webp" });
  });

  it.each([
    ["shot.png", "shot.png"],
    ["./shot.png", "shot.png"],
    ["shot.JPEG", "shot.JPEG"],
    ["images/my%20shot.png", "images/my%20shot.png"],
    ["images/my%2520shot.png", "images/my%2520shot.png"],
  ])("resolves %s without decoding twice", async (path, encoded) => {
    renderMarkdown(`![Screenshot](${path})`);
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(fetchMock.mock.calls[0][0]).toContain(`/filesystem/${encoded}?scope=workspace`);
  });

  it.each([".env", "build.log", "picture", "diagram.svg", "favicon.ico"])(
    "does not read unsupported image path %s",
    async (path) => {
      renderMarkdown(`![Screenshot](${path})`);
      expect(await screen.findByText(path)).toBeVisible();
      expect(fetchMock).not.toHaveBeenCalled();
      expect(document.querySelector("img")).toBeNull();
    },
  );

  it.each([
    "https://example.com/shot.png",
    "//example.com/shot.png",
    "data:image/png;base64,iVBORw0KGgo=",
    "blob:untrusted",
    "file:///workspace/project/shot.png",
    "%2F%2Fexample.com/shot.png",
    "https%3A%2F%2Fexample.com/shot.png",
    "/outside/shot.png",
    "/workspace/project/../shot.png",
    "../shot.png",
    "images/%2e%2e/shot.png",
  ])("does not fetch or expose an image source for %s", async (path) => {
    renderMarkdown(`![Screenshot](${path})`);
    await waitFor(() => expect(document.body.textContent).toMatch(/shot\.png|Image blocked/));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(document.querySelector("img")).toBeNull();
  });

  it("keeps images disabled for other markdown surfaces", async () => {
    renderMarkdown("![Screenshot](shot.png)", { enabled: false });
    expect(await screen.findByText("[Image blocked: Screenshot]")).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("drops srcset and forged workspace-image markers before rendering", async () => {
    renderMarkdown(
      '<img src="shot.png" alt="Screenshot" srcset="https://example.com/remote.png 1x">' +
        '<omnigent-workspace-image data-omnigent-image="outside.png"></omnigent-workspace-image>' +
        '<img src="https://example.com/remote.png" data-omnigent-image="forged.png" alt="Remote">',
    );
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.querySelector("[srcset]")).toBeNull();
    expect(document.querySelectorAll("img")).toHaveLength(1);
  });

  it.each([
    ["missing", () => new Response("missing", { status: 404 })],
    ["denied", () => new Response("denied", { status: 403 })],
    ["offline", () => new Response("offline", { status: 503 })],
    ["old server", () => imageResponse({}, false)],
    ["SVG", () => imageResponse({ content_type: "image/svg+xml" })],
    ["HTML", () => imageResponse({ content_type: "text/html" })],
    ["empty MIME type", () => imageResponse({ content_type: "" })],
    ["unknown MIME type", () => imageResponse({ content_type: "application/octet-stream" })],
    ["truncated", () => imageResponse({ truncated: true })],
    ["oversized", () => imageResponse({ bytes: MAX_IMAGE_BYTES + 1 })],
    [
      "oversized encoding with a small declared size",
      () => imageResponse({ content: "A".repeat(MAX_ENCODED_LENGTH + 4) }),
    ],
    [
      "oversized decoded bytes with a small declared size",
      () => imageResponse({ content: "A".repeat(MAX_ENCODED_LENGTH) }),
    ],
    ["invalid base64", () => imageResponse({ content: "invalid!" })],
    ["rejected request", () => new Error("connection closed")],
  ])("keeps a filename chip for %s", async (_name, response) => {
    let finishRequest!: (response: Response) => void;
    let rejectRequest!: (error: Error) => void;
    const request = new Promise<Response>((resolve, reject) => {
      finishRequest = resolve;
      rejectRequest = reject;
    });
    fetchMock.mockReturnValue(request);
    renderMarkdown("![Screenshot](shot.png)");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await act(async () => {
      const result = response();
      if (result instanceof Error) rejectRequest(result);
      else finishRequest(result);
      await request.catch(() => {});
    });
    expect(screen.getByText("shot.png")).toBeVisible();
    expect(createObjectURL).not.toHaveBeenCalled();
    expect(document.querySelector("img")).toBeNull();
  });

  it("accepts exactly 10 MiB of decoded image bytes", async () => {
    fetchMock.mockResolvedValue(
      imageResponse({ content: btoa("\0".repeat(MAX_IMAGE_BYTES)), bytes: MAX_IMAGE_BYTES }),
    );
    renderMarkdown("![Screenshot](shot.png)");
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(createObjectURL.mock.calls[0][0]).toMatchObject({
      size: MAX_IMAGE_BYTES,
      type: "image/png",
    });
  });

  it("falls back when image decoding fails", async () => {
    renderMarkdown("![Screenshot](shot.png)");
    const zoom = await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    fireEvent.error(zoom.querySelector("img")!);
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:workspace-image-1");
    expect(screen.getByText("shot.png")).toBeVisible();
    expect(document.querySelector("img")).toBeNull();
  });

  it("uses the side chat's session and workspace instead of its parent's", async () => {
    workspaceEnvironment.mockReturnValue({ data: { root: "/workspace/side", home: "/home/user" } });
    renderMarkdown("![Screenshot](/workspace/side/shot.png)", { scopedId: "session-side" });
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(workspaceEnvironment).toHaveBeenCalledWith("session-side");
    expect(fetchMock.mock.calls[0][0]).toContain("/sessions/session-side/");
  });

  it("uses the grid pane's own workspace without fetching another environment", async () => {
    renderMarkdown("![Screenshot](/workspace/grid/shot.png)", {
      scopedId: "session-grid",
      viewer: { ...VIEWER, conversationId: "session-grid", workspaceRoot: "/workspace/grid" },
    });
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(workspaceEnvironment).toHaveBeenCalledWith(undefined);
    expect(fetchMock.mock.calls[0][0]).toContain("/sessions/session-grid/");
  });

  it("waits for the workspace root and never fetches without a session", async () => {
    const result = renderMarkdown("![Screenshot](/workspace/project/shot.png)", {
      viewer: { ...VIEWER, workspaceRoot: null },
    });
    expect(await screen.findByText("shot.png")).toBeVisible();
    expect(fetchMock).not.toHaveBeenCalled();
    result.update("![Screenshot](/workspace/project/shot.png)");
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    result.update("![Screenshot](other.png)", { ...VIEWER, conversationId: undefined });
    expect(await screen.findByText("other.png")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.querySelector("img")).toBeNull();
  });

  it("aborts replaced reads, ignores late results and revokes old blobs", async () => {
    let finishFirst!: (response: Response) => void;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<Response>((resolve) => {
          finishFirst = resolve;
        }),
    );
    const result = renderMarkdown("![Screenshot](first.png)");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const firstSignal = fetchMock.mock.calls[0][1].signal as AbortSignal;
    result.update("![Screenshot](second.png)");
    await screen.findByRole("button", { name: "Zoom image: Screenshot" });
    expect(firstSignal.aborted).toBe(true);
    await act(async () => finishFirst(imageResponse()));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:workspace-image-1",
    );
    expect(revokeObjectURL).not.toHaveBeenCalled();
    result.update("![Screenshot](third.png)", { ...VIEWER, conversationId: "session-other" });
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));
    expect(screen.getByRole("img", { name: "Screenshot" })).toHaveAttribute(
      "src",
      "blob:workspace-image-2",
    );
    expect(revokeObjectURL.mock.calls).toEqual([["blob:workspace-image-1"]]);
    expect(fetchMock.mock.calls[2][0]).toContain("/sessions/session-other/");
    result.unmount();
    expect(revokeObjectURL.mock.calls).toEqual([
      ["blob:workspace-image-1"],
      ["blob:workspace-image-2"],
    ]);
  });
});
