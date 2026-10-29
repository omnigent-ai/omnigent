import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

const fetchHost = vi.fn();
const open = vi.fn();
const revealFile = vi.fn();
let client: QueryClient;

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  fetchHost
    .mockReset()
    .mockImplementation(async () => new Response(JSON.stringify({ ssh_target: "arclet_dev.ssh" })));
  open.mockReset();
  revealFile.mockReset().mockResolvedValue(true);
  vi.stubGlobal("fetch", fetchHost);
  vi.stubGlobal("open", open);
  vi.stubGlobal("omnigentDesktop", {
    kind: "electron",
    getHostIdentity: async () => ({ cliInstalled: true, hostId: "local" }),
    revealFile,
  });
});

afterEach(() => {
  cleanup();
  client.clear();
  vi.unstubAllGlobals();
  document.querySelectorAll("iframe").forEach((frame) => frame.remove());
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
  hostId: string | null = "local",
  directory = false,
) {
  const { FileViewerContext } = await import("./FileViewerContext");
  const { OpenInMenu } = await import("./OpenInMenu");
  let context = contextFor(hostId);
  let isDirectory = directory;
  const tree = (nextPath: string) => (
    <QueryClientProvider client={client}>
      <FileViewerContext.Provider value={context}>
        <OpenInMenu path={nextPath} directory={isDirectory} />
      </FileViewerContext.Provider>
    </QueryClientProvider>
  );
  const view = render(tree(path));
  await act(() => Promise.resolve());
  return {
    changeTarget: (nextPath: string, nextHost: string | null, nextDirectory = directory) => {
      isDirectory = nextDirectory;
      context = contextFor(nextHost);
      view.rerender(tree(nextPath));
    },
  };
}

async function openMenu(index = 0) {
  fireEvent.pointerDown(
    (await screen.findAllByRole("button", { name: "Choose app to open in" }))[index],
    { button: 0 },
  );
}

it("opens the default editor directly using a local deep link and bundled icon", async () => {
  await renderMenu();
  const primary = await screen.findByRole("button", { name: "Open in VS Code" });
  expect(primary).toHaveTextContent("Open");
  expect(primary.querySelector("svg")).not.toBeNull();
  fireEvent.click(primary);
  expect(open).toHaveBeenCalledWith(
    "vscode://file/workspace/src/main.ts",
    "_blank",
    "noopener,noreferrer",
  );
  expect(screen.queryByRole("menu")).toBeNull();
});

it.each(["arca.ssh", "arclet_dev.ssh", "me@devbox"])(
  "opens remote folders/files through %s",
  async (target) => {
    fetchHost.mockImplementation(async () => new Response(JSON.stringify({ ssh_target: target })));
    const { changeTarget } = await renderMenu("/workspace", "remote", true);
    fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
    expect(open.mock.calls[0][0]).toBe(
      `vscode://vscode-remote/ssh-remote+${encodeURIComponent(target)}/workspace`,
    );
    changeTarget("src/a #.ts", "remote", false);
    await openMenu();
    expect(screen.queryByRole("menuitem", { name: /Finder|file manager/ })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Cursor" }));
    expect(open.mock.calls[1][0]).toBe(
      `cursor://vscode-remote/ssh-remote+${encodeURIComponent(target)}/workspace/src/a%20%23.ts:1:1`,
    );
    expect(fetchHost.mock.calls[0][0]).toContain("/v1/hosts/remote?require_live=true");
  },
);

it("opens remote browser links without a native bridge", async () => {
  vi.stubGlobal("omnigentDesktop", undefined);
  await renderMenu("/workspace", "remote", true);
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(document.querySelector("iframe")).toHaveAttribute(
    "src",
    "vscode://vscode-remote/ssh-remote+arclet_dev.ssh/workspace",
  );
  expect(open).not.toHaveBeenCalled();
});

it("shares and remembers the editor preference across both controls", async () => {
  await renderMenu("/workspace", "local", true);
  await renderMenu();
  await openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: "Cursor" }));
  expect(screen.getAllByRole("button", { name: "Open in Cursor" })).toHaveLength(2);
  fireEvent.click(screen.getAllByRole("button", { name: "Open in Cursor" })[1]);
  expect(open.mock.calls.at(-1)?.[0]).toBe("cursor://file/workspace/src/main.ts");
  expect(localStorage.getItem("omnigent:default-code-editor")).toBe("cursor");
  cleanup();
  await renderMenu();
  expect(await screen.findByRole("button", { name: "Open in Cursor" })).toBeVisible();
});

it.each(["local", "remote"])(
  "resolves a late inherited %s host for both controls",
  async (hostId) => {
    const { setSessionHost, setSessionParent } = await import("@/lib/sessionHost");
    setSessionParent("conv_1", "parent");
    await renderMenu("/workspace", null, true);
    await renderMenu("src/main.ts", null);
    expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
    await act(() => setSessionHost("parent", hostId));
    const buttons = await screen.findAllByRole("button", { name: "Open in VS Code" });
    expect(buttons).toHaveLength(2);
    buttons.forEach((button) => fireEvent.click(button));
    const location = hostId === "local" ? "file" : "vscode-remote/ssh-remote+arclet_dev.ssh";
    expect(open.mock.calls.map(([url]) => url)).toEqual([
      `vscode://${location}/workspace`,
      `vscode://${location}/workspace/src/main.ts${hostId === "remote" ? ":1:1" : ""}`,
    ]);
  },
);

it("keeps local reveal available", async () => {
  await renderMenu("/workspace/packages", "local", true);
  await openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: /^Open in / }));
  expect(revealFile).toHaveBeenCalledWith("local", "/workspace/packages");
});

it.each([undefined, null, "", "bad target"])(
  "hides unknown remote destinations: %j",
  async (ssh_target) => {
    fetchHost.mockImplementation(async () => new Response(JSON.stringify({ ssh_target })));
    await renderMenu("/workspace", "remote", true);
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  },
);

it("clears disconnected targets and recovers after a host update", async () => {
  await renderMenu("/workspace", "remote", true);
  expect(await screen.findByRole("button", { name: "Open in VS Code" })).toBeVisible();
  fetchHost.mockImplementation(async () => new Response("", { status: 409 }));
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull());
  fetchHost.mockImplementation(
    async () => new Response(JSON.stringify({ ssh_target: "arclet_new.ssh" })),
  );
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toBe(
    "vscode://vscode-remote/ssh-remote+arclet_new.ssh/workspace",
  );
});

it("does not reuse a pending destination when switching hosts", async () => {
  let finish!: (response: Response) => void;
  fetchHost.mockImplementation(
    () =>
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
  );
  const { changeTarget } = await renderMenu("/workspace", "first", true);
  await waitFor(() => expect(fetchHost).toHaveBeenCalled());
  fetchHost.mockImplementation(
    async () => new Response(JSON.stringify({ ssh_target: "arclet_second.ssh" })),
  );
  changeTarget("/second", "second");
  await act(() => finish(new Response(JSON.stringify({ ssh_target: "arclet_first.ssh" }))));
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls[0][0]).toBe("vscode://vscode-remote/ssh-remote+arclet_second.ssh/second");
});

it.each(["ios", "android"])("hides desktop editor choices in the %s shell", async (kind) => {
  vi.stubGlobal("omnigentDesktop", undefined);
  vi.stubGlobal("omnigentNative", { kind });
  await renderMenu("/workspace", "remote", true);
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  expect(fetchHost).not.toHaveBeenCalled();
});
