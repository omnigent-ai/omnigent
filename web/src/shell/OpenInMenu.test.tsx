import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { toast } from "sonner";

vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));

const openFileInApp = vi.fn();
const getFileOpenApps = vi.fn();
const revealFile = vi.fn();
const openRemoteFileInApp = vi.fn();
const getFileOpenSshHost = vi.fn();
const fetchHost = vi.fn();

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  openFileInApp.mockReset().mockResolvedValue(true);
  revealFile.mockReset().mockResolvedValue(true);
  openRemoteFileInApp.mockReset().mockResolvedValue(true);
  getFileOpenSshHost.mockReset().mockResolvedValue(null);
  fetchHost.mockReset().mockResolvedValue({ ok: true, json: async () => ({}) });
  vi.stubGlobal("fetch", fetchHost);
  getFileOpenApps.mockReset().mockResolvedValue([
    { id: "vscode", label: "VS Code" },
    { id: "cursor", label: "Cursor" },
    { id: "terminal", label: "Terminal", directory: true },
    { id: "default", label: "Default app" },
  ]);
  vi.stubGlobal("omnigentDesktop", {
    kind: "electron",
    getHostIdentity: () => Promise.resolve({ cliInstalled: true, hostId: "local" }),
    getFileOpenApps,
    openFileInApp,
    revealFile,
    openRemoteFileInApp,
    getFileOpenSshHost,
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

function contextFor(sessionHostId: string) {
  return {
    openFile: vi.fn(),
    registerNavigationGuard: () => () => {},
    openGithubTab: vi.fn(),
    isChangedPath: () => false,
    conversationId: "conv_1",
    workspaceRoot: "/workspace",
    workspaceHome: null,
    sessionHostId,
  };
}

async function renderMenu(path = "src/main.ts", sessionHostId = "local", directory = false) {
  const { FileViewerContext } = await import("./FileViewerContext");
  const { OpenInMenu } = await import("./OpenInMenu");
  let context = contextFor(sessionHostId);
  const tree = (nextPath: string) => (
    <FileViewerContext.Provider value={context}>
      <OpenInMenu path={nextPath} directory={directory} />
    </FileViewerContext.Provider>
  );
  const view = render(tree(path));
  await act(() => Promise.resolve());
  return {
    changePath: (nextPath: string) => view.rerender(tree(nextPath)),
    changeHost: (hostId: string) => {
      context = contextFor(hostId);
      view.rerender(tree(path));
    },
  };
}

async function openMenu() {
  fireEvent.pointerDown(await screen.findByRole("button", { name: "Open in…" }), { button: 0 });
}

it("opens the current file in an editor and follows file navigation", async () => {
  const { changePath } = await renderMenu();
  await openMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: "VS Code" }));
  expect(openFileInApp).toHaveBeenCalledWith("local", "/workspace/src/main.ts", "vscode");
  changePath("/other folder/notes.md");
  await openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Default app" }));
  expect(openFileInApp).toHaveBeenLastCalledWith("local", "/other folder/notes.md", "default");
});

it("opens the browsed folder in a terminal or the file manager", async () => {
  await renderMenu("/workspace/packages", "local", true);
  await openMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Terminal" }));
  expect(openFileInApp).toHaveBeenCalledWith("local", "/workspace/packages", "terminal");
  await openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: /^Open in / }));
  expect(revealFile).toHaveBeenCalledWith("local", "/workspace/packages");
});

it.each(["browser", "old shell", "partial bridge", "remote host on old shell"])(
  "hides unavailable actions for %s",
  async (mode) => {
    if (mode === "browser") vi.stubGlobal("omnigentDesktop", undefined);
    if (mode !== "browser") {
      vi.stubGlobal("omnigentDesktop", {
        kind: "electron",
        getHostIdentity: () => Promise.resolve({ cliInstalled: true, hostId: "local" }),
        revealFile,
        ...(mode !== "old shell" ? { getFileOpenApps } : {}),
        ...(mode === "remote host on old shell" ? { openFileInApp } : {}),
      });
    }
    await renderMenu("src/main.ts", mode === "remote host on old shell" ? "remote" : "local");
    expect(screen.queryByRole("button", { name: "Open in…" })).toBeNull();
    expect(getFileOpenApps).not.toHaveBeenCalled();
  },
);

it("opens a verified Arca workspace through SSH and hides local-only apps", async () => {
  getFileOpenSshHost.mockResolvedValue("arca.ssh");
  await renderMenu("/home/user/project", "remote", true);
  await openMenu();
  expect(screen.queryByRole("menuitem", { name: "Terminal" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: "Default app" })).toBeNull();
  expect(screen.queryByRole("menuitem", { name: /Finder|file manager/ })).toBeNull();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  await waitFor(() =>
    expect(openRemoteFileInApp).toHaveBeenCalledWith(
      "remote",
      "/home/user/project",
      "cursor",
      "arca.ssh",
      true,
    ),
  );
  expect(openFileInApp).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
  expect(fetchHost).not.toHaveBeenCalled();
});

it("discovers an existing Arca connection from host fingerprints without prompting", async () => {
  const fingerprints = ["SHA256:LPJNul+wow4m6DsqxbninhsWHlwfp0JecwQzYpOLmCQ"];
  fetchHost.mockResolvedValue({
    ok: true,
    json: async () => ({ ssh_host_key_fingerprints: fingerprints }),
  });
  getFileOpenSshHost.mockImplementation((_host, keys) => Promise.resolve(keys ? "arca.ssh" : null));
  await renderMenu("/home/user/project", "remote", true);
  await openMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  await waitFor(() =>
    expect(openRemoteFileInApp).toHaveBeenCalledWith(
      "remote",
      "/home/user/project",
      "cursor",
      "arca.ssh",
      true,
    ),
  );
  expect(fetchHost).toHaveBeenCalledWith(
    "/v1/hosts/remote",
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
  expect(getFileOpenSshHost).toHaveBeenLastCalledWith("remote", fingerprints);
  expect(screen.queryByRole("dialog")).toBeNull();
});

it.each(["old server", "unreachable", "unmatched", "no discovery bridge"])(
  "hides remote opening when discovery is %s",
  async (mode) => {
    if (mode === "unreachable") fetchHost.mockRejectedValue(new Error("timeout"));
    if (mode === "unmatched")
      fetchHost.mockResolvedValue({
        ok: true,
        json: async () => ({ ssh_host_key_fingerprints: ["SHA256:unknown"] }),
      });
    if (mode === "no discovery bridge")
      vi.stubGlobal("omnigentDesktop", {
        kind: "electron",
        getFileOpenApps,
        openRemoteFileInApp,
        getHostIdentity: () => Promise.resolve({ cliInstalled: true, hostId: "local" }),
      });
    await renderMenu("src/main.ts", "remote");
    await act(() => Promise.resolve());
    expect(screen.queryByRole("button", { name: "Open in…" })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(openRemoteFileInApp).not.toHaveBeenCalled();
    if (mode === "no discovery bridge") expect(fetchHost).not.toHaveBeenCalled();
  },
);

it("waits for SSH discovery before showing the remote picker", async () => {
  let complete!: (alias: string | null) => void;
  getFileOpenSshHost.mockImplementation(
    () =>
      new Promise((resolve) => {
        complete = resolve;
      }),
  );
  await renderMenu("src/main.ts", "remote");
  expect(screen.queryByRole("button", { name: "Open in…" })).toBeNull();
  await act(() => complete("arca.ssh"));
  await openMenu();
  expect(screen.queryByRole("menuitem", { name: "SSH connection…" })).toBeNull();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  expect(openRemoteFileInApp).toHaveBeenCalledWith(
    "remote",
    "/workspace/src/main.ts",
    "cursor",
    "arca.ssh",
    false,
  );
});

it("ignores stale SSH discovery when the session changes hosts", async () => {
  let finishOld!: (alias: string | null) => void;
  getFileOpenSshHost.mockImplementation((hostId) =>
    hostId === "remote"
      ? new Promise((resolve) => {
          finishOld = resolve;
        })
      : Promise.resolve(null),
  );
  const { changeHost } = await renderMenu("src/main.ts", "remote");
  changeHost("other");
  await act(() => finishOld("arca.ssh"));
  expect(screen.queryByRole("button", { name: "Open in…" })).toBeNull();
  expect(openRemoteFileInApp).not.toHaveBeenCalled();
});

it("reports a remote launch failure without retrying the path locally", async () => {
  getFileOpenSshHost.mockResolvedValue("arca.ssh");
  openRemoteFileInApp.mockRejectedValue(new Error("Cannot connect"));
  await renderMenu("src/main.ts", "remote");
  await openMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  await waitFor(() =>
    expect(toast.error).toHaveBeenCalledWith("Couldn't open this remote file in Cursor"),
  );
  expect(openFileInApp).not.toHaveBeenCalled();
});

it("reports a failed native launch without an unhandled rejection", async () => {
  openFileInApp.mockRejectedValue(new Error("App unavailable"));
  await renderMenu();
  await openMenu();
  fireEvent.click(await screen.findByRole("menuitem", { name: "VS Code" }));
  await waitFor(() =>
    expect(toast.error).toHaveBeenCalledWith("Couldn't open this file in VS Code"),
  );
});

it("keeps file-manager access if app discovery fails", async () => {
  getFileOpenApps.mockRejectedValue(new Error("Disconnected"));
  await renderMenu();
  await openMenu();
  expect(screen.queryByRole("menuitem", { name: "VS Code" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: /^Show in / }));
  expect(revealFile).toHaveBeenCalledWith("local", "/workspace/src/main.ts");
});
