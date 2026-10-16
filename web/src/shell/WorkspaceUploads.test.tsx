import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Profiler, type ReactNode } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { cancelUpload, enqueueUploads, transferUpload } from "@/lib/workspaceUploadController";
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
let identity = "alice";
vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => identity }));
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
let panelConversation = "upload-panel";
vi.mock("@/lib/routing", () => ({ useParams: () => ({ conversationId: panelConversation }) }));
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
function view(directory = "src", content?: ReactNode, picker = true, sessionId = "s") {
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUploadProvider sessionId={sessionId}>
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
  panelConversation = "upload-panel";
  identity = "alice";
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
  vi.restoreAllMocks();
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
      new Response(
        JSON.stringify({
          error: {
            code:
              statusCode === 401
                ? "unauthorized"
                : statusCode === 403
                  ? "permission_denied"
                  : statusCode === 429
                    ? "busy"
                    : "runner_unavailable",
          },
        }),
        {
          status: statusCode,
          headers: { "Retry-After": "2" },
        },
      ),
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
    new Response(
      JSON.stringify({ error: { code: statusCode === 429 ? "busy" : "runner_unavailable" } }),
      { status: statusCode, headers: { "Retry-After": "2" } },
    ),
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
  view("src", tree());
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
  expect(screen.getByRole("button", { name: "Retry a.txt" })).toBeEnabled();
  act(() => useUploadStore.getState().update(row.key, { cancellationPending: true }));
  expect(screen.getByRole("button", { name: "Retry a.txt" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Retry failed" })).toBeNull();
  act(() => useUploadStore.getState().update(row.key, { cancellationPending: false }));
  expect(screen.getByRole("button", { name: "Retry a.txt" })).toBeEnabled();
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

it("shows server-busy waits apart from network reconnects in the tray", async () => {
  let creates = 0;
  vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) => {
    if (init?.method !== "POST") return new Response(JSON.stringify(capability));
    creates += 1;
    if (creates === 1) throw new TypeError("Failed to fetch");
    if (creates === 2)
      return new Response(JSON.stringify({ error: { code: "busy", message: "busy" } }), {
        status: 429,
      });
    return new Response(JSON.stringify({ error: { code: "unsupported", message: "No" } }), {
      status: 501,
    });
  });
  view();
  await screen.findByRole("button", { name: "Upload files" });
  const row = {
    origin: window.location.origin,
    identity: "alice",
    session: "s",
    environment: "default",
    key: "upload",
    id: "upload",
    batch: "batch",
    file: new File(["x"], "a.txt"),
    name: "a.txt",
    size: 1,
    directory: "src",
    destination: "src",
    offset: 0,
    controller: new AbortController(),
    limits,
    status: "queued" as const,
    conflict: "fail" as const,
  };
  act(() => useUploadStore.getState().add([row]));
  let release!: () => void;
  const wait = () =>
    new Promise<void>((resolve) => {
      release = resolve;
    });
  const transfer = transferUpload(row.key, client, wait);
  expect(await screen.findByText("Reconnecting…")).toBeTruthy();
  act(() => release());
  expect(await screen.findByText("Server busy. Waiting to retry… (HTTP 429 busy)")).toBeTruthy();
  expect(screen.queryByText("Reconnecting…")).toBeNull();
  act(() => release());
  await act(() => transfer);
  expect(useUploadStore.getState().rows.upload.status).toBe("failed");
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

it("sends Changes drops to the workspace root after browsing a subfolder", async () => {
  // A fresh conversation keeps FilesPanel's per-session browse cache isolated.
  panelConversation = "upload-changes-root";
  const shell = (flatView: boolean) => (
    <QueryClientProvider client={client}>
      <WorkspaceUploadProvider sessionId="s">
        <WorkspaceUploadDropRoot>{panel(flatView)}</WorkspaceUploadDropRoot>
      </WorkspaceUploadProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(shell(false));
  await screen.findByRole("button", { name: "Upload files" });
  fireEvent.doubleClick(screen.getByText("other/"));
  const transfer = { types: ["Files"], files: [new File(["bytes"], "root.txt")] };
  const header = screen.getByRole("button", { name: "Refresh files" }).closest(".h-11");
  fireEvent.drop(header as HTMLElement, { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenLastCalledWith(
    expect.anything(),
    transfer.files,
    "other",
    "other",
    limits,
    client,
  );
  rerender(shell(true));
  const row = await screen.findByText("existing.txt");
  fireEvent.dragOver(row, { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(screen.getByText("to demo")).toBeVisible();
  expect(screen.queryByText("to other")).toBeNull();
  fireEvent.drop(row, { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenLastCalledWith(
    expect.anything(),
    transfer.files,
    "",
    "demo",
    limits,
    client,
  );
  expect(enqueueUploads).toHaveBeenCalledTimes(2);
});

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
    if (surface === "header") {
      expect(screen.getAllByRole("status")).toHaveLength(2);
      expect(screen.getAllByRole("status")[1]).toBeEmptyDOMElement();
    }
    fireEvent.dragOver(target, { dataTransfer: transfer });
    expect(screen.getByText("Upload to workspace")).toBeVisible();
    expect(
      screen
        .getAllByRole("status")
        .some((region) => region.textContent === `Upload to ${usesPanel ? "demo" : "src"}`),
    ).toBe(true);
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

it("keeps one empty live region mounted and announces each tree drop destination", async () => {
  view("", tree({ browseLocation: "" }));
  await screen.findByRole("button", { name: "Upload files" });
  const regions = screen.getAllByRole("status");
  expect(regions).toHaveLength(2);
  const [liveRegion, rootRegion] = regions;
  expect(rootRegion).toBeEmptyDOMElement();
  expect(liveRegion).toHaveAttribute("aria-live", "polite");
  expect(liveRegion).toBeEmptyDOMElement();
  const transfer = { types: ["Files"], files: [new File(["bytes"], "new.txt")] };

  fireEvent.dragOver(screen.getByText("assets/"), { dataTransfer: transfer });
  expect(screen.getAllByRole("status")).toContain(liveRegion);
  expect(liveRegion).toHaveTextContent("Upload to assets");
  fireEvent.dragOver(screen.getByText("existing.txt"), { dataTransfer: transfer });
  expect(liveRegion).toHaveTextContent("Upload to assets");
  fireEvent.dragOver(screen.getByText("other/"), { dataTransfer: transfer });
  expect(liveRegion).toHaveTextContent("Upload to other");
  fireEvent.dragOver(document.querySelector("[data-upload-tree]") as HTMLElement, {
    dataTransfer: transfer,
  });
  expect(liveRegion).toHaveTextContent("Upload to workspace root");
  fireEvent.dragEnd(screen.getByText("other/"), { dataTransfer: transfer });
  expect(screen.getAllByRole("status")).toContain(liveRegion);
  expect(liveRegion).toBeEmptyDOMElement();

  fireEvent.dragOver(screen.getByText("assets/"), { dataTransfer: transfer });
  fireEvent.dragLeave(screen.getByText("assets/"), { dataTransfer: transfer });
  expect(liveRegion).toBeEmptyDOMElement();
  fireEvent.dragOver(screen.getByText("assets/"), { dataTransfer: transfer });
  fireEvent.drop(screen.getByText("assets/"), { dataTransfer: transfer });
  expect(liveRegion).toBeEmptyDOMElement();
  fireEvent.dragOver(screen.getByText("other/"), { dataTransfer: transfer });
  fireEvent.dragEnd(document, { dataTransfer: transfer });
  expect(liveRegion).toBeEmptyDOMElement();

  fireEvent.dragOver(screen.getByTestId("files-content"), { dataTransfer: transfer });
  expect(screen.getAllByRole("status")).toContain(rootRegion);
  expect(rootRegion).toHaveTextContent("Upload to demo");
  expect(liveRegion).toBeEmptyDOMElement();
  fireEvent.dragEnd(document, { dataTransfer: transfer });
  expect(rootRegion).toBeEmptyDOMElement();
});

it("swaps the folder icon and tints its row, then freezes the DROP row despite stale hover", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const assets = screen.getByText("assets/");
  const transfer = { types: ["Files"], files: [new File(["bytes"], "new.txt")] };
  fireEvent.dragOver(assets, { dataTransfer: transfer });
  const folder = assets.closest("[data-drop-folder]") as HTMLElement;
  expect(folder).toHaveAttribute("data-upload-hover");
  expect(folder).toHaveClass("w-full", "bg-info/10");
  expect(folder.querySelector("svg.lucide-upload")).toBeTruthy();
  expect(folder.querySelector("svg.lucide-folder, svg.lucide-folder-open")).toBeNull();
  expect(assets).toHaveClass("text-foreground");
  expect(folder).not.toHaveClass("ring-2");
  expect(screen.getByText("Upload to src/assets", { selector: '[role="status"]' })).toBeVisible();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
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
  expect(folder).not.toHaveClass("bg-info/10");
  expect(folder.querySelector("svg.lucide-upload")).toBeNull();
});

it("shows the dashed root zone while a root-level file row is the drop target", async () => {
  view(
    "src",
    tree({
      files: [
        { path: "top.txt", name: "top.txt", type: "file", bytes: 1, modified_at: null },
        { path: "other", name: "other", type: "directory", bytes: null, modified_at: null },
      ],
    }),
  );
  await screen.findByRole("button", { name: "Upload files" });
  const transfer = { types: ["Files"], files: [new File(["bytes"], "new.txt")] };
  fireEvent.dragOver(screen.getByText("top.txt"), { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(screen.getByText("to src")).toBeVisible();
  expect(screen.getByText("Upload to src", { selector: '[role="status"]' })).toBeVisible();
  const rootCue = document.querySelector('[data-drop-variant="upload"]') as HTMLElement;
  expect(rootCue).toHaveClass("pointer-events-none", "border-dashed", "bg-transparent");
  expect(rootCue).not.toHaveClass("bg-background/90");
  expect(screen.getByText("other/").closest("[data-drop-folder]")).toBeInTheDocument();
  expect(document.querySelector("[data-upload-root-outline]")).toBeNull();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
  fireEvent.dragOver(screen.getByText("other/"), { dataTransfer: transfer });
  const folder = screen.getByText("other/").closest("[data-drop-folder]");
  expect(folder).toHaveAttribute("data-upload-hover");
  expect(folder).toHaveClass("w-full", "bg-info/10");
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
  expect(document.querySelector("[data-upload-root-outline]")).toBeNull();
  fireEvent.drop(screen.getByText("top.txt"), { dataTransfer: transfer });
  expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
    expect.anything(),
    transfer.files,
    "src",
    "src",
    limits,
    client,
  );
  expect(document.querySelector("[data-upload-root-outline]")).toBeNull();
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
      async () =>
        new Response(JSON.stringify({ error: { code: "runner_unavailable" } }), { status: 503 }),
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
  const folder = row.closest("[data-drop-folder]");
  expect(folder).toHaveAttribute("data-upload-hover");
  expect(folder).toHaveClass("bg-info/10");
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
  fireEvent.dragEnter(header, { dataTransfer: transfer });
  fireEvent.dragLeave(row, { dataTransfer: transfer });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(folder).not.toHaveClass("bg-info/10");
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
  expect(screen.getByText("Upload to src/dir0", { selector: '[role="status"]' })).toBeVisible();
  const before = commits.mock.calls.length;
  for (let index = 0; index < 25; index++) fireEvent.dragOver(row, { dataTransfer: transfer });
  expect(commits).toHaveBeenCalledTimes(before);
});

it("highlights the parent folder when hovering its file and restores it on drag end", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const transfer = { types: ["Files"] };
  const folder = screen.getByText("assets/").closest("[data-drop-folder]") as HTMLElement;
  fireEvent.dragOver(screen.getByText("existing.txt"), { dataTransfer: transfer });
  expect(folder).toHaveAttribute("data-upload-hover");
  expect(folder).toHaveClass("w-full", "bg-info/10");
  expect(folder.querySelector("svg.lucide-upload")).toBeTruthy();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(0);
  fireEvent.dragEnd(screen.getByText("existing.txt"), { dataTransfer: transfer });
  expect(folder).not.toHaveAttribute("data-upload-hover");
  expect(folder).not.toHaveClass("bg-info/10");
  expect(folder.querySelector("svg.lucide-upload")).toBeNull();
});

it("names an offscreen parent folder in the dashed fallback zone", async () => {
  view("src", tree());
  await screen.findByRole("button", { name: "Upload files" });
  const folder = screen.getByText("assets/").closest("[data-index]");
  expect(folder).toBeTruthy();
  folder?.remove();
  fireEvent.dragOver(screen.getByText("existing.txt"), {
    dataTransfer: { types: ["Files"] },
  });
  expect(screen.getByText("Upload to workspace")).toBeVisible();
  expect(screen.getByText("to src/assets")).toBeVisible();
  expect(screen.getByText("Upload to src/assets", { selector: '[role="status"]' })).toBeVisible();
  expect(document.querySelectorAll('[data-drop-variant="upload"]')).toHaveLength(1);
});

it.each([429, 503])(
  "keeps the capability %s alert stable throughout automatic retries",
  async (statusCode) => {
    vi.useFakeTimers();
    vi.mocked(authenticatedFetch).mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ error: { code: statusCode === 429 ? "busy" : "runner_unavailable" } }),
          { status: statusCode, headers: { "Retry-After": "2" } },
        ),
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
    vi.useFakeTimers();
    vi.mocked(authenticatedFetch).mockRejectedValue(new TypeError("Failed to fetch"));
    view();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry upload availability" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByLabelText("Choose workspace files")).toBeNull();
    expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
    expect(
      fireEvent.drop(screen.getByTestId("files-content"), {
        dataTransfer: { types: ["Files"], files: [new File(["x"], "a")] },
      }),
    ).toBe(true);
    expect(enqueueUploads).not.toHaveBeenCalled();
    vi.mocked(authenticatedFetch).mockResolvedValue(
      supported ? new Response(JSON.stringify(capability)) : new Response(null, { status: 404 }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.queryByRole("button", { name: "Retry upload availability" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    if (supported) expect(screen.getByRole("button", { name: "Upload files" })).toBeEnabled();
    else {
      expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
      expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
    }
  },
);

it.each([500, 502, 504, 429, 503, "transport", "generic JSON"])(
  "keeps unknown capability failure %s hidden until a quiet retry succeeds",
  async (failure) => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0.5);
    if (failure === "transport")
      vi.mocked(authenticatedFetch).mockRejectedValue(new TypeError("Failed to fetch"));
    else
      vi.mocked(authenticatedFetch).mockResolvedValue(
        new Response(
          failure === "generic JSON"
            ? JSON.stringify({ error: { code: "unavailable" } })
            : "<html>Bad Gateway</html>",
          { status: failure === "generic JSON" ? 502 : Number(failure) },
        ),
      );
    view("src", tree());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry upload availability" })).toBeNull();
    expect(screen.queryByLabelText("Choose workspace files")).toBeNull();
    expect(
      document.querySelector("[data-upload-drop-root], [data-upload-disabled-reason]"),
    ).toBeNull();
    const row = screen.getByText("assets/");
    const transfer = { types: ["Files"], files: [new File(["a"], "a.txt")] };
    fireEvent.dragEnter(row, { dataTransfer: transfer });
    expect(fireEvent.dragOver(row, { dataTransfer: transfer })).toBe(true);
    expect(document.querySelector("[data-upload-tree], [data-upload-hover]")).toBeNull();
    expect(document.querySelector('[data-drop-variant="upload"]')).toBeNull();
    expect(fireEvent.drop(row, { dataTransfer: transfer })).toBe(true);
    expect(enqueueUploads).not.toHaveBeenCalled();
    expect(authenticatedFetch).toHaveBeenCalledTimes(1);
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Upload files" })).toBeEnabled();
    expect(document.querySelector("[data-upload-drop-root]")).toBeTruthy();
    expect(fireEvent.dragOver(row, { dataTransfer: transfer })).toBe(false);
    expect(document.querySelector("[data-upload-hover]")).toBeTruthy();
    expect(screen.getByText("Upload to src/assets", { selector: '[role="status"]' })).toBeVisible();
    expect(fireEvent.drop(row, { dataTransfer: transfer })).toBe(false);
    expect(enqueueUploads).toHaveBeenCalledExactlyOnceWith(
      expect.anything(),
      transfer.files,
      "src/assets",
      "src/assets",
      limits,
      client,
    );
  },
);

it("ends quiet gateway retries after the existing five-retry budget", async () => {
  vi.useFakeTimers();
  vi.mocked(authenticatedFetch).mockImplementation(async () => new Response(null, { status: 502 }));
  view();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100_000);
  });
  expect(authenticatedFetch).toHaveBeenCalledTimes(6);
  expect(screen.queryByRole("alert")).toBeNull();
  expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
});

it("retains actionable diagnostics for a known route during a gateway outage", async () => {
  view();
  await screen.findByRole("button", { name: "Upload files" });
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(null, { status: 502 }));
  await act(async () => {
    await client.invalidateQueries({
      queryKey: ["workspace-upload-capability"],
      refetchType: "none",
    });
    void client.refetchQueries({ queryKey: ["workspace-upload-capability"] });
  });
  const alert = await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Upload files" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Retry upload availability" })).toHaveAttribute(
    "aria-describedby",
    alert.id,
  );
});

it.each(["identity", "session"])(
  "does not carry route evidence across a new %s",
  async (boundary) => {
    const rendered = view();
    await screen.findByRole("button", { name: "Upload files" });
    rendered.unmount();
    vi.useFakeTimers();
    vi.mocked(authenticatedFetch).mockRejectedValue(new TypeError("Failed to fetch"));
    if (boundary === "identity") identity = "bob";
    view("src", undefined, true, boundary === "session" ? "new-session" : "s");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Upload files" })).toBeNull();
    expect(document.querySelector("[data-upload-drop-root]")).toBeNull();
  },
);

it("keeps typed route evidence and a stable alert through a later gateway retry", async () => {
  vi.useFakeTimers();
  vi.mocked(authenticatedFetch)
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ error: { code: "busy" } }), {
        status: 429,
        headers: { "Retry-After": "1" },
      }),
    )
    .mockImplementation(async () => new Response(null, { status: 502 }));
  view();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(100);
  });
  const alert = screen.getByRole("alert");
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1000);
  });
  expect(screen.getByRole("alert")).toBe(alert);
  expect(screen.getByRole("button", { name: "Upload files" })).toBeDisabled();
  expect(alert).toHaveTextContent(/could not check upload availability/i);
});

it("Cancel remaining leaves 100 queued files unadmitted while reconciling two in-flight creates", async () => {
  const controller = await vi.importActual<typeof UploadController>(
    "@/lib/workspaceUploadController",
  );
  vi.mocked(cancelUpload).mockImplementation(controller.cancelUpload);
  view();
  await screen.findByRole("button", { name: "Upload files" });
  vi.useFakeTimers();
  const admissions: ((response: Response) => void)[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (_url, init) => {
    if (init?.method === "POST")
      return new Promise<Response>((resolve) => {
        admissions.push(resolve);
      });
    return new Response(null, { status: 204 });
  });
  const owner = {
    origin: window.location.origin,
    identity: "alice",
    session: "s",
    environment: "default",
  };
  let creates = 0;
  let pending = 0;
  try {
    act(() =>
      controller.enqueueUploads(
        owner,
        Array.from({ length: 102 }, (_, index) => new File([new Uint8Array(20)], `file-${index}`)),
        "src",
        "src",
        { ...limits, max_files_per_batch: 102, max_batch_bytes: 2040 },
        client,
      ),
    );
    expect(admissions).toHaveLength(2);
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Cancel remaining" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    creates = admissions.length;
    const rows = Object.values(useUploadStore.getState().rows);
    pending = rows.filter((item) => item.cancellationPending).length;
    expect(rows.every((item) => item.status === "cancelled" && !item.file)).toBe(true);
  } finally {
    await act(async () => {
      admissions.forEach((resolve, index) =>
        resolve(new Response(JSON.stringify({ upload_id: `late-${index}`, offset: 0 }))),
      );
      await vi.advanceTimersByTimeAsync(300);
      controller.discardUploads(owner.origin, owner.identity);
    });
    vi.mocked(cancelUpload).mockReset();
  }
  expect(creates).toBe(2);
  expect(pending).toBe(2);
  expect(
    vi.mocked(authenticatedFetch).mock.calls.filter(([, init]) => init?.method === "DELETE"),
  ).toHaveLength(2);
});
