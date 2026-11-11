import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";

const fetchHost = vi.fn();
const open = vi.fn();
const browserOpen = vi.fn();
const revealFile = vi.fn();
let client: QueryClient;
let serverHosts: Host[];

function host(hostId: string, name: string, sandboxProvider: string | null = null): Host {
  return {
    host_id: hostId,
    name,
    owner: "owner@example.test",
    status: "online",
    sandbox_provider: sandboxProvider,
  };
}

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
  client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  serverHosts = [];
  fetchHost.mockReset().mockImplementation(async (input: string) => {
    if (input === "/v1/hosts") {
      return new Response(JSON.stringify({ hosts: serverHosts }));
    }
    return new Response("", { status: 404 });
  });
  open.mockReset();
  browserOpen.mockReset();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    browserOpen(this.href);
  });
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

function contextFor(sessionHostId: string | null, conversationId = "conv_1") {
  return {
    openFile: vi.fn(),
    registerNavigationGuard: () => () => {},
    openGithubTab: vi.fn(),
    isChangedPath: () => false,
    conversationId,
    workspaceRoot: "/workspace",
    workspaceHome: null,
    sessionHostId,
  };
}

async function renderMenu(
  path = "src/main.ts",
  hostId: string | null = "local",
  directory = false,
  conversationId = "conv_1",
) {
  const { FileViewerContext } = await import("./FileViewerContext");
  const { OpenInMenu } = await import("./OpenInMenu");
  let context = contextFor(hostId, conversationId);
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
    changeTarget: (
      nextPath: string,
      nextHost: string | null,
      nextDirectory = directory,
      nextConversation = conversationId,
    ) => {
      isDirectory = nextDirectory;
      context = contextFor(nextHost, nextConversation);
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
  // The host list may load before the asynchronous desktop identity resolves.
  expect(fetchHost.mock.calls.every(([input]) => input === "/v1/hosts")).toBe(true);
});

it.each([
  ["arbitrary remote", host("remote", "dev-box")],
  ["managed sandbox", host("remote", "workspace-display-name", "public-sandbox")],
  ["offline remote", { ...host("remote", "offline-box"), status: "offline" as const }],
])("does not treat a %s display name as an SSH target", async (_case, remoteHost) => {
  serverHosts = [remoteHost];
  await renderMenu("/workspace", "remote", true);
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  expect(open).not.toHaveBeenCalled();
  expect(fetchHost).not.toHaveBeenCalled();
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

it("resolves a late inherited local host for both controls", async () => {
  const { setSessionHost, setSessionParent } = await import("@/lib/sessionHost");
  setSessionParent("conv_1", "parent");
  await renderMenu("/workspace", null, true);
  await renderMenu("src/main.ts", null);
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  await act(() => setSessionHost("parent", "local"));
  const buttons = await screen.findAllByRole("button", { name: "Open in VS Code" });
  expect(buttons).toHaveLength(2);
  buttons.forEach((button) => fireEvent.click(button));
  expect(open.mock.calls.map(([url]) => url)).toEqual([
    "vscode://file/workspace",
    "vscode://file/workspace/src/main.ts",
  ]);
});

it("keeps local reveal available", async () => {
  await renderMenu("/workspace/packages", "local", true);
  await openMenu();
  fireEvent.click(screen.getByRole("menuitem", { name: /^Open in / }));
  expect(revealFile).toHaveBeenCalledWith("local", "/workspace/packages");
});

it("shows actions when switching from a remote host to the local host", async () => {
  const { changeTarget } = await renderMenu("/workspace", "remote", true);
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  changeTarget("/local", "local");
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toBe("vscode://file/local");
});

it.each(["ios", "android"])("hides desktop editor choices in the %s shell", async (kind) => {
  vi.stubGlobal("omnigentDesktop", undefined);
  vi.stubGlobal("omnigentNative", { kind });
  await renderMenu("/workspace", "remote", true);
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  expect(fetchHost).not.toHaveBeenCalled();
});
