import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { enqueueUploads } from "@/lib/workspaceUploadController";
import { useUploadStore } from "@/store/uploadStore";
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
vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: { permissionLevel: permission, status: "idle" } }),
}));
let workspaceRoot: string | null = "/work/demo";
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceEnvironment: () => ({ data: { root: workspaceRoot } }),
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => "idle" }));
vi.mock("@/lib/workspaceUploadController", () => ({
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
function view(directory = "src") {
  return render(
    <QueryClientProvider client={client}>
      <WorkspaceUploadProvider sessionId="s">
        <WorkspaceUploadDropRoot>
          <UploadPicker directory={directory} />
          <span data-testid="files-content">tree</span>
        </WorkspaceUploadDropRoot>
        <UploadsTray visible />
      </WorkspaceUploadProvider>
    </QueryClientProvider>,
  );
}
beforeEach(() => {
  permission = 4;
  workspaceRoot = "/work/demo";
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.mocked(authenticatedFetch).mockResolvedValue(new Response(JSON.stringify(capability)));
});
afterEach(() => {
  cleanup();
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
  expect(uploadGate(4, capability, "ended", "").enabled).toBe(false);
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
