import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Profiler, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { enqueueUploads } from "@/lib/workspaceUploadController";
import type * as UploadController from "@/lib/workspaceUploadController";
import { useUploadStore } from "@/store/uploadStore";
import { FolderTree } from "./FolderTree";
import { FilesPanel } from "./FilesPanel";
import type * as WorkspaceFiles from "@/hooks/useWorkspaceChangedFiles";
import type { SessionStatus } from "@/lib/types";
import {
  droppedUploadFiles,
  resolveUploadTarget,
  uploadGate,
  UploadPicker,
  UploadsTray,
  WorkspaceUploadDropRoot,
  WorkspaceUploadProvider,
} from "./WorkspaceUploads";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "alice" }));
let permission: number | null = 4;
let archived = false;
let status: SessionStatus = "idle";
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { permissionLevel: permission, status, archived } }),
}));
let workspaceRoot: string | null = "/work/demo";
const directoryData = new Map();
const panelFiles = [
  { path: "assets/existing.txt", name: "existing.txt", type: "file", bytes: 1, modified_at: null },
  { path: "other", name: "other", type: "directory", bytes: null, modified_at: null },
];
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceFiles>()),
  useWorkspaceEnvironment: () => ({ data: { root: workspaceRoot } }),
  useWorkspaceDirectories: () => directoryData,
  useWorkspaceAllFiles: () => ({
    data: { data: panelFiles },
    isLoading: false,
    isError: false,
    error: null,
  }),
  useWorkspaceChangedFiles: () => ({
    data: { data: [{ ...panelFiles[0], status: "modified" }] },
    isLoading: false,
    isError: false,
    error: null,
  }),
  useWorkspaceFileSearch: () => ({
    data: undefined,
    isFetching: false,
    isError: false,
    error: null,
  }),
}));
vi.mock("@/lib/routing", () => ({ useParams: () => ({ conversationId: "upload-panel" }) }));
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: () => true,
  useSessionHostOnline: () => true,
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => undefined }));
vi.mock("@/lib/workspaceUploadController", async (importOriginal) => ({
  ...(await importOriginal<typeof UploadController>()),
  enqueueUploads: vi.fn(),
  cancelUpload: vi.fn(),
  retryUpload: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), dismiss: vi.fn() }) }));
const limits = {
  chunk: { min: 2, initial: 4, max: 8 },
  max_file_bytes: 100,
  max_files_per_batch: 10,
  max_batch_bytes: 200,
  max_open_uploads: 16,
  max_open_bytes: 200,
  parallel_files: 2,
};
const capability = { supported: true, limits };
let client: QueryClient;
function view(directory = "src", content?: ReactNode, picker = true) {
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUploadProvider sessionId="s">
        <WorkspaceUploadDropRoot>
          {picker && <UploadPicker directory={directory} />}
          <span data-testid="files-content">tree</span>
          {content}
        </WorkspaceUploadDropRoot>
        <UploadsTray visible />
      </WorkspaceUploadProvider>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  permission = 4;
  archived = false;
  status = "idle";
  workspaceRoot = "/work/demo";
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
  client.clear();
  useUploadStore.setState({ rows: {} });
});

it.each([null, 1])("keeps upload controls pending or hidden for permission %s", async (level) => {
  permission = level;
  view();
  expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
  expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
  expect(authenticatedFetch).not.toHaveBeenCalled();
  expect(uploadGate(level, capability, "idle", "").visible).toBe(false);
});

it("hides every upload affordance and does not intercept rail drops when capability returns 404", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 404 }));
  view();
  await waitFor(() =>
    expect(
      client.getQueryData(["workspace-upload-capability", window.location.origin, "alice", "s"]),
    ).toBeNull(),
  );
  expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
  expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
  expect(
    fireEvent.drop(screen.getByTestId("files-content"), {
      dataTransfer: { types: ["Files"], files: [new File(["x"], "a")] },
    }),
  ).toBe(true);
  expect(enqueueUploads).not.toHaveBeenCalled();
});

it("explains unsupported runners, allows asleep connections, and disables failed or outside-workspace targets", async () => {
  expect(
    uploadGate(4, { ...capability, supported: false, reason: "runner_unavailable" }, "idle", "")
      .enabled,
  ).toBe(true);
  expect(uploadGate(4, capability, "failed", "").enabled).toBe(false);
  expect(uploadGate(4, capability, "idle", "/tmp").reason).toContain("inside");
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(
      JSON.stringify({ ...capability, supported: false, reason: "runner_capability_mismatch" }),
    ),
  );
  view();
  const button = await screen.findByRole("button", { name: "Upload files" });
  expect((button as HTMLButtonElement).disabled).toBe(true);
  expect(button.parentElement?.title).toContain("restart");
});

it.each([
  ["failed", false],
  ["idle", true],
])("disables uploads for status %s, archived %s", async (sessionStatus, isArchived) => {
  status = sessionStatus as SessionStatus;
  archived = Boolean(isArchived);
  view();
  const button = await screen.findByRole("button", { name: "Upload files" });
  expect(button).toBeDisabled();
  expect(button.parentElement?.title).toMatch(/reconnect|archived/i);
  fireEvent.drop(screen.getByTestId("files-content"), {
    dataTransfer: { types: ["Files"], files: [new File(["x"], "a")] },
  });
  expect(enqueueUploads).not.toHaveBeenCalled();
});

it.each([401, 403, 429, 503])(
  "explains capability %s and recovers only after a successful retry",
  async (statusCode) => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(JSON.stringify({ error: { code: "unavailable" } }), {
        status: statusCode,
        headers: { "Retry-After": "2" },
      }),
    );
    view();
    const button = await screen.findByRole("button", { name: "Upload files" });
    expect(button).toBeDisabled();
    expect(button.parentElement?.title).toMatch(
      statusCode === 401
        ? /sign in/i
        : statusCode === 403
          ? /permission/i
          : statusCode === 429
            ? /busy|throttl/i
            : /unavailable/i,
    );
    const retry = screen.getByRole("button", { name: "Retry upload availability" });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      statusCode === 401
        ? /sign in/i
        : statusCode === 403
          ? /permission/i
          : statusCode === 429
            ? /busy/i
            : /unavailable/i,
    );
    expect(retry).toHaveAttribute("aria-describedby", alert.id);
    fireEvent.drop(screen.getByTestId("files-content"), {
      dataTransfer: { types: ["Files"], files: [new File(["x"], "a")] },
    });
    expect(enqueueUploads).not.toHaveBeenCalled();
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
    if (statusCode === 401 || statusCode === 403) fireEvent.click(retry);
    await waitFor(() => expect(button).toBeEnabled(), { timeout: 4000 });
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

it.each([429, 503])("honours Retry-After before re-probing capability %s", async (statusCode) => {
  vi.useFakeTimers();
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(null, { status: statusCode, headers: { "Retry-After": "2" } }),
  );
  view();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  expect(screen.getByRole("button", { name: "Upload files" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Retry upload availability" })).toBeDisabled();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1800);
  });
  expect(authenticatedFetch).toHaveBeenCalledTimes(1);
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(200);
  });
  expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Upload files" })).toBeEnabled();
});

it("freezes the picker directory on opening, accepts multiple files, resets and restores focus on change or cancel", async () => {
  const rendered = view("src");
  const button = await screen.findByRole("button", { name: "Upload files" });
  fireEvent.click(button);
  const input = screen.getByLabelText("Choose workspace files") as HTMLInputElement;
  expect(input.multiple).toBe(true);
  expect(input.accept).toBe("");
  rendered.rerender(
    <QueryClientProvider client={client}>
      <WorkspaceUploadProvider sessionId="s">
        <WorkspaceUploadDropRoot>
          <UploadPicker directory="other" />
          <span data-testid="files-content" />
        </WorkspaceUploadDropRoot>
        <UploadsTray visible />
      </WorkspaceUploadProvider>
    </QueryClientProvider>,
  );
  const files = [new File(["a"], "a.txt"), new File(["b"], "b.txt")];
  fireEvent.change(input, { target: { files } });
  expect(enqueueUploads).toHaveBeenCalledWith(
    expect.objectContaining({ session: "s", identity: "alice" }),
    files,
    "src",
    "src",
    limits,
    client,
  );
  expect(input.value).toBe("");
  expect(document.activeElement).toBe(button);
  button.blur();
  fireEvent(input, new Event("cancel"));
  expect(document.activeElement).toBe(button);
});

it("drops into the browsed directory with upload-only copy, accepting file siblings and refusing folders", async () => {
  view();
  await screen.findByRole("button", { name: "Upload files" });
  const target = screen.getByTestId("files-content");
  const file = new File(["x"], "a.txt");
  fireEvent.dragOver(target, { dataTransfer: { types: ["Files"] } });
  expect(screen.getByText("Upload to workspace")).toBeTruthy();
  expect(screen.getByText("to src")).toBeTruthy();
  expect(screen.queryByText("Attach to message")).toBeNull();
  fireEvent.drop(target, { dataTransfer: { types: ["Files"], files: [file] } });
  expect(enqueueUploads).toHaveBeenCalledWith(
    expect.anything(),
    [file],
    "src",
    "src",
    limits,
    client,
  );
  const directory = new File([], "folder");
  const event = {
    dataTransfer: {
      items: [{ webkitGetAsEntry: () => ({ isDirectory: true }), getAsFile: () => directory }],
    },
  } as unknown as DragEvent;
  expect(droppedUploadFiles(event, [directory, file])).toEqual([file]);
  expect(resolveUploadTarget(event, "browsed")).toBe("browsed");
});

it("shows accessible file-specific progress, failure and cancel controls in a durable tray", async () => {
  view();
  await screen.findByRole("button", { name: "Upload files" });
  const owner = {
    origin: window.location.origin,
    identity: "alice",
    session: "s",
    environment: "default",
  };
  const row = {
    ...owner,
    key: "upload",
    id: "upload",
    batch: "batch",
    file: new File(["x"], "a.txt"),
    name: "a.txt",
    size: 100,
    directory: "src",
    destination: "src",
    offset: 50,
    controller: new AbortController(),
    limits,
    status: "uploading" as const,
    conflict: "fail" as const,
  };
  useUploadStore.getState().add([row]);
  await screen.findByRole("region", { name: "Uploads" });
  expect(screen.getByRole("progressbar", { name: "a.txt upload progress" })).toBeTruthy();
  expect(screen.getByRole("progressbar", { name: "a.txt upload progress" })).toHaveAttribute(
    "aria-valuenow",
    "50",
  );
  expect(screen.getByRole("button", { name: "Cancel a.txt" })).toBeTruthy();
  useUploadStore.getState().update(row.key, { status: "failed", error: "No space" });
  expect(await screen.findByRole("alert")).toHaveProperty("textContent", "No space");
  expect(screen.getByRole("button", { name: "Retry a.txt" })).toBeTruthy();
  useUploadStore.getState().update(row.key, {
    status: "cancelled",
    file: undefined,
    cancellationPending: false,
    error: "Server did not confirm cancellation. Temporary staging expires automatically.",
  });
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(/server did not confirm cancellation/i),
  );
  expect(screen.getByRole("button", { name: "Dismiss" })).toBeEnabled();
});

it("allows an asleep runner to connect before workspace metadata is available", async () => {
  workspaceRoot = null;
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(JSON.stringify({ ...capability, supported: false, reason: "runner_unavailable" })),
  );
  view("");
  const button = await screen.findByRole("button", { name: "Upload files" });
  expect(button).toBeEnabled();
  expect(document.querySelector("[data-upload-drop-root]")).toBeTruthy();
});

it.each([
  ["folder", "src/assets", "browsed/src/assets"],
  ["file", "src/assets", "browsed/src/assets"],
  ["root-file", "", "browsed"],
])("resolves %s from the drop event's mounted row", (_kind, rowDirectory, wanted) => {
  const container = document.createElement("section");
  container.dataset.uploadTree = "";
  const row = container.appendChild(document.createElement("div"));
  row.dataset.dropDir = rowDirectory;
  const child = row.appendChild(document.createElement("span"));
  expect(resolveUploadTarget({ target: child } as unknown as DragEvent, "browsed")).toBe(wanted);
});

function panel(flatView = false) {
  return (
    <FilesPanel
      flatView={flatView}
      onFileSelect={vi.fn()}
      showHidden={false}
      onShowHiddenChange={vi.fn()}
      sort="alpha"
      onSortChange={vi.fn()}
    />
  );
}

it.each(["space", "header", "empty tree", "Changes"])(
  "drops on the real %s surface into the browsed root",
  async (surface) => {
    const usesPanel = surface === "header" || surface === "Changes";
    view(
      usesPanel ? "" : "src",
      usesPanel
        ? panel(surface === "Changes")
        : tree(surface === "empty tree" ? { files: [] } : {}),
      !usesPanel,
    );
    await screen.findByRole("button", { name: "Upload files" });
    const target =
      surface === "header"
        ? (screen.getByRole("button", { name: "Refresh files" }).closest(".h-11") as HTMLElement)
        : surface === "Changes"
          ? screen.getByText("existing.txt")
          : surface === "empty tree"
            ? screen.getByText("No files in workspace")
            : (document.querySelector("[data-upload-tree]") as HTMLElement);
    const directory = usesPanel ? "" : "src";
    const transfer = { types: ["Files"], files: [new File(["bytes"], "root.txt")] };
    fireEvent.dragOver(target, { dataTransfer: transfer });
    expect(screen.getByText("Upload to workspace")).toBeVisible();
    expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
    fireEvent.drop(target, { dataTransfer: transfer });
    expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
      expect.anything(),
      transfer.files,
      directory,
      usesPanel ? "demo" : directory,
      limits,
      client,
    );
  },
);

function tree(props: Partial<Parameters<typeof FolderTree>[0]> = {}) {
  return (
    <FolderTree
      files={[
        {
          path: "assets/existing.txt",
          name: "existing.txt",
          type: "file",
          bytes: 1,
          modified_at: null,
        },
        { path: "other", name: "other", type: "directory", bytes: null, modified_at: null },
      ]}
      conversationId="upload-tree"
      browseLocation="src"
      isLoading={false}
      isError={false}
      error={null}
      onFileSelect={vi.fn()}
      showHidden={false}
      changedFiles={undefined}
      sort="alpha"
      {...props}
    />
  );
}

it("shows a single folder ring and upload label, then freezes the DROP row despite stale hover", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const assets = screen.getByText("assets/");
  const transfer = { types: ["Files"], files: [new File(["bytes"], "new.txt")] };
  fireEvent.dragOver(assets, { dataTransfer: transfer });
  expect(screen.getByText("Upload to src/assets")).toBeVisible();
  expect(screen.getByText("Upload to src/assets").parentElement).toBe(document.body);
  expect(assets.closest("[data-upload-hover]")).toBeTruthy();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
  expect(screen.queryByText("Upload to workspace")).toBeNull();
  fireEvent.drop(screen.getByText("other/"), { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
    expect.anything(),
    transfer.files,
    "src/other",
    "src/other",
    limits,
    client,
  );
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
  expect(assets.closest("[data-upload-hover]")).toBeNull();
});

it("uploads a file-row drop to its parent once and a header drop to the browsed root", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const transfer = { types: ["Files"], files: [new File(["bytes"], "new.txt")] };
  fireEvent.drop(screen.getByText("existing.txt"), { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
    expect.anything(),
    transfer.files,
    "src/assets",
    "src/assets",
    limits,
    client,
  );
  fireEvent.dragOver(screen.getByTestId("files-content"), { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
  fireEvent.drop(screen.getByTestId("files-content"), { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenLastCalledWith(
    expect.anything(),
    transfer.files,
    "src",
    "src",
    limits,
    client,
  );
  expect(enqueueUploads).toHaveBeenCalledTimes(2);
});

it("spring-load expands the actual folder without re-rooting the browser", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const folder = screen.getByRole("button", { name: "other/" });
  expect(folder).toHaveAttribute("aria-expanded", "false");
  fireEvent.dragOver(folder, { dataTransfer: { types: ["Files"] } });
  await waitFor(() => expect(folder).toHaveAttribute("aria-expanded", "true"));
  fireEvent.drop(folder, {
    dataTransfer: { types: ["Files"], files: [new File(["a"], "new.txt")] },
  });
  expect(enqueueUploads).toHaveBeenLastCalledWith(
    expect.anything(),
    expect.anything(),
    "src/other",
    "src/other",
    limits,
    client,
  );
});

it("a disabled tree never highlights or handles a row drop", async () => {
  vi.mocked(authenticatedFetch).mockResolvedValue(
    new Response(
      JSON.stringify({ ...capability, supported: false, reason: "runner_capability_mismatch" }),
    ),
  );
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const row = screen.getByText("assets/");
  const transfer = { types: ["Files"], files: [new File(["a"], "a.txt")] };
  expect(fireEvent.dragOver(row, { dataTransfer: transfer })).toBe(true);
  expect(document.querySelector("[data-upload-hover]")).toBeNull();
  expect(document.querySelector('[data-drop-variant="upload"]')).toBeNull();
  expect(fireEvent.drop(row, { dataTransfer: transfer })).toBe(true);
  expect(enqueueUploads).not.toHaveBeenCalled();
});

it("backs off successive unavailable capability probes without Retry-After", async () => {
  vi.useFakeTimers();
  const random = vi.spyOn(Math, "random").mockReturnValue(0.5);
  try {
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(null, { status: 503 }),
    );
    view();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400);
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Upload files" })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800);
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button", { name: "Upload files" })).toBeEnabled();
  } finally {
    random.mockRestore();
  }
});

it.each(["directory", "file"])(
  "search %s rows keep feedback and destination at the browsed root",
  async (kind) => {
    view(
      "src",
      tree({
        searchQuery: "deep",
        searchResults: [
          { path: "a/b/deep", name: "deep", type: "directory", bytes: null, modified_at: null },
          { path: "x/y/deep.txt", name: "deep.txt", type: "file", bytes: 1, modified_at: null },
        ],
      }),
    );
    await screen.findByRole("button", { name: "Upload files" });
    const row = screen.getByText(kind === "directory" ? "a/b/deep/" : "x/y/deep.txt");
    const transfer = { types: ["Files"], files: [new File(["x"], "search.txt")] };
    fireEvent.dragOver(row, { dataTransfer: transfer });
    expect(screen.getByText("Upload to workspace")).toBeVisible();
    expect(screen.getByText("to src")).toBeVisible();
    expect(document.querySelector("[data-upload-hover]")).toBeNull();
    fireEvent.drop(row, { dataTransfer: transfer });
    expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
      expect.anything(),
      transfer.files,
      "src",
      "src",
      limits,
      client,
    );
  },
);

it("keeps one overlay across tree to header and header-child crossings", async () => {
  view("", panel(), false);
  await screen.findByRole("button", { name: "Upload files" });
  const row = screen.getByText("assets/");
  const child = screen.getByRole("button", { name: "Refresh files" });
  const header = child.closest(".h-11") as HTMLElement;
  const transfer = { types: ["Files"] };
  fireEvent.dragEnter(row, { dataTransfer: transfer });
  fireEvent.dragOver(row, { dataTransfer: transfer });
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
  fireEvent.dragEnter(header, { dataTransfer: transfer });
  fireEvent.dragLeave(row, { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
  fireEvent.dragEnter(child, { dataTransfer: transfer });
  fireEvent.dragLeave(header, { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  fireEvent.dragLeave(child, { dataTransfer: transfer });
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
});

it("does not commit the expanded tree again for same-row dragover", async () => {
  const files = Array.from({ length: 120 }, (_, index) => ({
    path: `dir${index}/existing.txt`,
    name: "existing.txt",
    type: "file" as const,
    bytes: 1,
    modified_at: null,
  }));
  const commits = vi.fn();
  client.setQueryData(
    ["workspace-upload-capability", window.location.origin, "alice", "s"],
    capability,
  );
  view(
    "src",
    <Profiler id="tree" onRender={commits}>
      {tree({ files, conversationId: "large-hover-tree" })}
    </Profiler>,
  );
  await screen.findByRole("button", { name: "Upload files" });
  const row = screen.getByText("dir0/");
  const transfer = { types: ["Files"] };
  fireEvent.dragOver(row, { dataTransfer: transfer });
  expect(screen.getByText("Upload to src/dir0")).toBeVisible();
  const before = commits.mock.calls.length;
  for (let index = 0; index < 25; index++) fireEvent.dragOver(row, { dataTransfer: transfer });
  expect(commits).toHaveBeenCalledTimes(before);
});

it("uses a solid background for the floating label over tree rows", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  fireEvent.dragOver(screen.getByText("assets/"), { dataTransfer: { types: ["Files"] } });
  expect(screen.getByText("Upload to src/assets")).toHaveClass("bg-card-solid");
});

it.each([429, 503])(
  "keeps the capability %s alert stable throughout automatic retries",
  async (statusCode) => {
    vi.useFakeTimers();
    vi.mocked(authenticatedFetch).mockImplementation(
      async () => new Response(null, { status: statusCode, headers: { "Retry-After": "2" } }),
    );
    view();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    const alert = screen.getByRole("alert");
    const explanation = alert.textContent;
    let changes = 0;
    const observer = new MutationObserver(() => {
      changes++;
    });
    observer.observe(alert, { childList: true, characterData: true, subtree: true });
    try {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000);
      });
      expect(authenticatedFetch).toHaveBeenCalledTimes(6);
      expect(screen.getByRole("alert")).toBe(alert);
      expect(alert.textContent).toBe(explanation);
      expect(changes).toBe(0);
      expect(screen.getByRole("button", { name: "Retry upload availability" })).toBeEnabled();
    } finally {
      observer.disconnect();
    }
  },
);

it.each([false, true])(
  "keeps a no-response capability failure pending until HTTP recovery, supported %s",
  async (supported) => {
    vi.mocked(authenticatedFetch).mockRejectedValue(new TypeError("Failed to fetch"));
    view();
    const retry = await screen.findByRole("button", { name: "Retry upload availability" });
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
    expect(screen.queryByLabelText("Choose workspace files")).toBeNull();
    expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/could not check upload availability/i);
    expect(retry).toHaveAttribute("aria-describedby", alert.id);
    expect(
      fireEvent.drop(screen.getByTestId("files-content"), {
        dataTransfer: { types: ["Files"], files: [new File(["x"], "a")] },
      }),
    ).toBe(true);
    expect(enqueueUploads).not.toHaveBeenCalled();
    vi.mocked(authenticatedFetch).mockResolvedValue(
      supported ? new Response(JSON.stringify(capability)) : new Response(null, { status: 404 }),
    );
    fireEvent.click(retry);
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Retry upload availability" })).toBeNull(),
    );
    expect(screen.queryByRole("alert")).toBeNull();
    if (supported) expect(screen.getByRole("button", { name: "Upload files" })).toBeEnabled();
    else {
      expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
      expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
    }
  },
);
