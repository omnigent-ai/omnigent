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

function contextFor(sessionHostId: string | null) {
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

async function renderMenu(
  path = "src/main.ts",
  sessionHostId: string | null = "local",
  directory = false,
) {
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
  fireEvent.pointerDown(await screen.findByRole("button", { name: "Choose app to open in" }), {
    button: 0,
  });
}

it("opens the default app directly and uses its artwork", async () => {
  const icon = "data:image/png;base64,aWNvbg==";
  getFileOpenApps.mockResolvedValue([{ id: "vscode", label: "VS Code", icon }]);
  await renderMenu();
  const primary = await screen.findByRole("button", { name: "Open in VS Code" });
  expect(primary.querySelector("img")).toHaveAttribute("src", icon);
  fireEvent.click(primary);
  expect(openFileInApp).toHaveBeenCalledWith("local", "/workspace/src/main.ts", "vscode");
  expect(screen.queryByRole("menu")).toBeNull();
});

it.each([
  ["local", false],
  ["local", true],
  ["remote", false],
  ["remote", true],
])(
  "opens both child-session controls on the inherited %s host (late: %s)",
  async (hostId, late) => {
    const { setSessionHost, setSessionParent } = await import("@/lib/sessionHost");
    setSessionParent("conv_1", "parent");
    getFileOpenSshHost.mockResolvedValue("arca.ssh");
    if (!late) setSessionHost("parent", hostId);
    await renderMenu("/workspace", null, true);
    await renderMenu("src/main.ts", null);
    if (late) {
      expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
      await act(() => setSessionHost("parent", hostId));
    }
    const buttons = await screen.findAllByRole("button", { name: "Open in VS Code" });
    expect(buttons).toHaveLength(2);
    buttons.forEach((button) => fireEvent.click(button));
    if (hostId === "local") {
      expect(openFileInApp.mock.calls).toEqual([
        [hostId, "/workspace", "vscode"],
        [hostId, "/workspace/src/main.ts", "vscode"],
      ]);
      expect(openRemoteFileInApp).not.toHaveBeenCalled();
    } else {
      await waitFor(() =>
        expect(openRemoteFileInApp.mock.calls).toEqual([
          [hostId, "/workspace", "vscode", "arca.ssh", true],
          [hostId, "/workspace/src/main.ts", "vscode", "arca.ssh", false],
        ]),
      );
      expect(openFileInApp).not.toHaveBeenCalled();
    }
  },
);

it("shares the selected default between folder and file buttons and remembers it", async () => {
  await renderMenu("/workspace", "local", true);
  await renderMenu("src/main.ts");
  fireEvent.pointerDown(screen.getAllByRole("button", { name: "Choose app to open in" })[0], {
    button: 0,
  });
  expect(openFileInApp).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  expect(screen.getAllByRole("button", { name: "Open in Cursor" })).toHaveLength(2);
  fireEvent.click(screen.getAllByRole("button", { name: "Open in Cursor" })[1]);
  expect(openFileInApp).toHaveBeenLastCalledWith("local", "/workspace/src/main.ts", "cursor");
  cleanup();
  await renderMenu();
  expect(await screen.findByRole("button", { name: "Open in Cursor" })).toBeInTheDocument();
});

it("keeps remote and local defaults separate and ignores unavailable choices", async () => {
  localStorage.setItem("omnigent:open-in-app:local", "terminal");
  localStorage.setItem("omnigent:open-in-app:remote", "removed-editor");
  getFileOpenSshHost.mockResolvedValue("arca.ssh");
  await renderMenu("src/main.ts", "remote");
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  await waitFor(() =>
    expect(openRemoteFileInApp).toHaveBeenCalledWith(
      "remote",
      "/workspace/src/main.ts",
      "vscode",
      "arca.ssh",
      false,
    ),
  );
  expect(localStorage.getItem("omnigent:open-in-app:local")).toBe("terminal");
});

it("opens the selected app even when saving the default fails", async () => {
  const save = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
    throw new Error("storage unavailable");
  });
  try {
    await renderMenu();
    await openMenu();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
    expect(openFileInApp).toHaveBeenCalledWith("local", "/workspace/src/main.ts", "cursor");
  } finally {
    save.mockRestore();
  }
});

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
    expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
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

it.each(["primary", "menu"])(
  "refreshes expired SSH discovery for the %s action",
  async (action) => {
    getFileOpenSshHost.mockResolvedValue("arca.ssh");
    await renderMenu("src/main.ts", "remote");
    const select = async () => {
      if (action === "menu") {
        await openMenu();
        fireEvent.click(await screen.findByRole("menuitem", { name: "VS Code" }));
      } else {
        fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
      }
    };

    const fingerprints = ["SHA256:new-host-key"];
    getFileOpenSshHost.mockImplementation((_host, keys) =>
      Promise.resolve(keys ? "arca_new.ssh" : null),
    );
    let provideMetadata!: (response: unknown) => void;
    fetchHost.mockReturnValueOnce(
      new Promise((resolve) => {
        provideMetadata = resolve;
      }),
    );
    await select();
    await waitFor(() => expect(fetchHost).toHaveBeenCalledTimes(1));
    expect(openRemoteFileInApp).not.toHaveBeenCalled();
    await act(() =>
      provideMetadata({
        ok: true,
        json: async () => ({ ssh_host_key_fingerprints: fingerprints }),
      }),
    );
    await waitFor(() =>
      expect(openRemoteFileInApp).toHaveBeenCalledWith(
        "remote",
        "/workspace/src/main.ts",
        "vscode",
        "arca_new.ssh",
        false,
      ),
    );
    expect(getFileOpenSshHost).toHaveBeenLastCalledWith("remote", fingerprints);

    getFileOpenSshHost.mockResolvedValue(null);
    fetchHost.mockRejectedValue(new Error("Host unavailable"));
    await select();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't open this remote file in VS Code"),
    );
    expect(openRemoteFileInApp).toHaveBeenCalledTimes(1);
    expect(openFileInApp).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  },
);

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
    expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
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
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  await act(() => complete("arca.ssh"));
  getFileOpenSshHost.mockResolvedValue("arca.ssh");
  await openMenu();
  expect(screen.queryByRole("menuitem", { name: "SSH connection…" })).toBeNull();
  fireEvent.click(await screen.findByRole("menuitem", { name: "Cursor" }));
  await waitFor(() =>
    expect(openRemoteFileInApp).toHaveBeenCalledWith(
      "remote",
      "/workspace/src/main.ts",
      "cursor",
      "arca.ssh",
      false,
    ),
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
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
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
