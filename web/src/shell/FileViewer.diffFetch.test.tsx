// Never-diffed types (image/PDF/model/binary) must not request a diff; deleted
// or content-errored files must still fetch theirs instead of hanging on a
// disabled query. The real useFileDiff runs against a stubbed fetch.

import { act, cleanup, render, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./CodeViewer", () => ({
  CodeViewer: ({ path }: { path: string }) => <div data-testid="code-viewer">{path}</div>,
}));

vi.mock("./CommentsPanel", () => ({
  CommentsPanel: () => <div data-testid="comments-panel" />,
}));

vi.mock("./MonacoDiffViewer", () => ({
  MonacoDiffViewer: () => <div data-testid="diff-viewer" />,
}));

vi.mock("@/hooks/useIsMobileViewport", () => ({
  useIsMobileViewport: () => false,
}));

vi.mock("@/hooks/useComments", () => ({
  useComments: () => ({ data: [] }),
  useAddComment: () => ({ mutate: vi.fn() }),
  useUpdateComment: () => ({ mutate: vi.fn() }),
  useDeleteComment: () => ({ mutate: vi.fn() }),
}));

vi.mock("@/hooks/useFileContent", () => ({
  useFileContent: vi.fn(),
  downloadWorkspaceFile: vi.fn(),
}));

vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceChangedFiles: vi.fn(),
  useWorkspaceServeable: () => true,
}));

vi.mock("@/hooks/useResizablePanel", () => ({
  useResizablePanel: () => ({
    panelWidth: 400,
    handleProps: {
      onMouseDown: vi.fn(),
      onKeyDown: vi.fn(),
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize panel",
      tabIndex: 0,
    },
    isDesktop: true,
  }),
}));

vi.mock("@/hooks/CommentSenderContext", () => ({
  CommentSenderProvider: ({ children }: { children: React.ReactNode }) => children,
  useOptionalCommentSender: () => null,
}));

vi.mock("@/store/chatStore", () => ({
  useChatStore: (selector: (s: { boundAgentId: null; status: string }) => unknown) =>
    selector({ boundAgentId: null, status: "idle" }),
}));

import { useFileContent } from "@/hooks/useFileContent";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { FileViewer } from "./FileViewer";

const DIFF_URL_MARKER = "/resources/environments/default/diff/";

const fetchMock = vi.fn<typeof fetch>(
  async () =>
    ({
      ok: true,
      status: 200,
      statusText: "OK",
      json: async () => ({
        object: "session.environment.filesystem.file_diff",
        path: "x",
        before: null,
        after: "",
      }),
    }) as unknown as Response,
);

type ChangedStatus = "created" | "modified" | "deleted";

interface OpenedFile {
  kind: string;
  path: string;
  content_type: string | null;
  encoding: "utf-8" | "base64";
  status?: ChangedStatus;
}

// How the file-content metadata request has settled, mirrored through the
// TanStack query shape FileViewer reads: "loading" is still pending (no data,
// no error), "error" has settled without data (e.g. a deleted file's 404).
type ContentPhase = "loading" | "error";

type OpenSpec = OpenedFile | { path: string; status?: ChangedStatus; phase: ContentPhase };

function setFileContent(spec: OpenSpec): void {
  const result =
    "phase" in spec
      ? { data: undefined, isPending: spec.phase === "loading" }
      : {
          data: {
            object: "session.environment.filesystem.file_content",
            path: spec.path,
            content_type: spec.content_type,
            encoding: spec.encoding,
            content: "AAAA",
            bytes: 4,
          },
          isPending: false,
        };
  vi.mocked(useFileContent).mockReturnValue(result as unknown as ReturnType<typeof useFileContent>);
}

function setChangedFile(path: string, status: ChangedStatus): void {
  vi.mocked(useWorkspaceChangedFiles).mockReturnValue({
    data: {
      available: true,
      data: [{ path, name: path, status, bytes: 4, modified_at: null }],
    },
  } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
}

function renderViewer(path: string): { queryClient: QueryClient; rerender: () => void } {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // Build a fresh element each render: React bails out of re-rendering an element
  // passed by the same reference, which would hide the mocked content update.
  const ui = () => (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <FileViewer open conversationId="conv_1" path={path} onClose={() => {}} />
      </MemoryRouter>
    </QueryClientProvider>
  );
  const result = render(ui());
  return { queryClient, rerender: () => result.rerender(ui()) };
}

function openChangedFile(spec: OpenSpec): QueryClient {
  setFileContent(spec);
  setChangedFile(spec.path, spec.status ?? "created");
  return renderViewer(spec.path).queryClient;
}

function diffUrl(path: string): string {
  return `/v1/sessions/conv_1/resources/environments/default/diff/${path}`;
}

// Flush a render/microtask cycle so any mount effect that enables the diff query
// has run before we treat an empty in-flight set as final; otherwise a disabled
// query reads as "done" immediately and a regressing enable could slip past.
async function diffRequests(queryClient: QueryClient): Promise<string[]> {
  await act(async () => {
    await Promise.resolve();
  });
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  return fetchMock.mock.calls
    .map(([input]) => String(input))
    .filter((url) => url.includes(DIFF_URL_MARKER));
}

beforeEach(() => {
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("FileViewer — diff fetch for files it never diffs", () => {
  const neverDiffed: OpenedFile[] = [
    // Text-encoded and extensionless, so only the image content-type suppresses
    // the diff — exercises the image gate independently of binary detection.
    { kind: "image", path: "diagram", content_type: "image/svg+xml", encoding: "utf-8" },
    { kind: "PDF", path: "sample.pdf", content_type: "application/pdf", encoding: "base64" },
    {
      kind: "3D model",
      path: "part.stl",
      content_type: "application/vnd.ms-pki.stl",
      encoding: "utf-8",
    },
    { kind: "video", path: "recording.mp4", content_type: "video/mp4", encoding: "base64" },
    { kind: "binary", path: "bundle.zip", content_type: "application/zip", encoding: "base64" },
    // Binary content typed only by its base64 encoding, with no extension the
    // classifier recognizes — the case the metadata gate must still suppress.
    { kind: "encoding-only binary", path: "datablob", content_type: null, encoding: "base64" },
  ];

  it.each(neverDiffed)("does not request a diff for a changed $kind file", async (file) => {
    expect(await diffRequests(openChangedFile(file))).toEqual([]);
  });

  it("does not request a diff while a changed file's metadata is still loading", async () => {
    // A text-like extension would classify as diffable, but the file could still
    // resolve to media/binary content; wait for the metadata before fetching.
    expect(await diffRequests(openChangedFile({ path: "notes.txt", phase: "loading" }))).toEqual(
      [],
    );
  });

  it("requests the diff only once a changed text file's metadata resolves", async () => {
    setChangedFile("notes.txt", "created");
    setFileContent({ path: "notes.txt", phase: "loading" });
    const { queryClient, rerender } = renderViewer("notes.txt");
    // Pending metadata: the extension alone must not trigger the fetch early.
    expect(await diffRequests(queryClient)).toEqual([]);

    setFileContent({
      kind: "text",
      path: "notes.txt",
      content_type: "text/plain",
      encoding: "utf-8",
    });
    rerender();
    expect(await diffRequests(queryClient)).toEqual([diffUrl("notes.txt")]);
  });

  it("still requests the diff for a changed text file", async () => {
    const queryClient = openChangedFile({
      kind: "text",
      path: "notes.txt",
      content_type: "text/plain",
      encoding: "utf-8",
    });
    expect(await diffRequests(queryClient)).toEqual([diffUrl("notes.txt")]);
  });

  it("still requests the diff for a deleted changed text file", async () => {
    // A deleted file's content request keeps retrying its 404, so its metadata
    // stays pending; the diff must still be fetched against previous contents.
    // The pending phase makes this fail if the deleted-file exception is removed.
    const queryClient = openChangedFile({
      path: "removed.txt",
      status: "deleted",
      phase: "loading",
    });
    expect(await diffRequests(queryClient)).toEqual([diffUrl("removed.txt")]);
  });

  it("still requests the diff when a changed text file's content request fails", async () => {
    // A failed content request must not leave the diff view permanently loading:
    // once the metadata settles, even as an error, the diffable file fetches its diff.
    const queryClient = openChangedFile({
      path: "notes.txt",
      status: "created",
      phase: "error",
    });
    expect(await diffRequests(queryClient)).toEqual([diffUrl("notes.txt")]);
  });
});
