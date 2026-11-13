import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Host } from "@/hooks/useHosts";

const fetchHost = vi.fn();
const open = vi.fn();
const browserOpen = vi.fn();
const revealFile = vi.fn();
let client: QueryClient;
let serverHosts: Host[];
let metadata: Map<string, object | number>;
const labelsUrl =
  "/v1/sessions/conv_1/resources/environments/default/filesystem/etc/metric_proxy/labels/default?base=host";
const arcletNameUrl =
  "/v1/sessions/conv_1/resources/environments/default/filesystem/opt/arclet/arclet-name?base=host";

function textFile(content: string) {
  return {
    object: "session.environment.filesystem.file_content",
    path: "/metadata",
    content_type: "text/plain",
    encoding: "utf-8",
    content,
    bytes: content.length,
  };
}

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
  metadata = new Map();
  fetchHost.mockReset().mockImplementation(async (input: string) => {
    if (input === "/v1/hosts") {
      return new Response(JSON.stringify({ hosts: serverHosts }));
    }
    const file = metadata.get(input);
    if (file !== undefined) {
      return typeof file === "number"
        ? new Response("", { status: file })
        : new Response(JSON.stringify(file));
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
  ["desktop", "arclet", "conversant-crane", "arclet_conversant-crane.ssh"],
  ["browser", "arclet", "conversant-crane", "arclet_conversant-crane.ssh"],
  ["desktop", "databricks-arca", "my-arca", "arca.ssh"],
  ["browser", "databricks-arca", "my-arca", "arca.ssh"],
  ["desktop", null, "ip-10-92-23-179", "arca.ssh"],
  ["browser", null, "custom display name", "arca.ssh"],
] as const)(
  "opens remote folders/files from %s for %s",
  async (platform, provider, name, sshTarget) => {
    serverHosts = [host("remote", name, provider)];
    if (provider === null) metadata.set(labelsUrl, textFile("arca_type arca\n"));
    if (platform === "browser") vi.stubGlobal("omnigentDesktop", undefined);
    const latestLink = () =>
      platform === "desktop" ? open.mock.calls.at(-1)?.[0] : browserOpen.mock.calls.at(-1)?.[0];
    const { changeTarget } = await renderMenu("/workspace", "remote", true);
    fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
    expect(latestLink()).toBe(`vscode://vscode-remote/ssh-remote+${sshTarget}/workspace`);
    changeTarget("src/a #.ts", "remote", false);
    await openMenu();
    expect(screen.queryByRole("menuitem", { name: /Finder|file manager/ })).toBeNull();
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "Cursor" }));
    expect(latestLink()).toBe(
      `cursor://vscode-remote/ssh-remote+${sshTarget}/workspace/src/a%20%23.ts:1:1`,
    );
    if (platform === "browser") expect(open).not.toHaveBeenCalled();
    expect(fetchHost.mock.calls.map(([input]) => input)).toEqual(
      provider === null ? ["/v1/hosts", labelsUrl] : ["/v1/hosts"],
    );
  },
);

it("shares canonical Arclet discovery across folder and file controls without fetching on click", async () => {
  serverHosts = [host("remote", "arbitrary display name")];
  metadata.set(labelsUrl, textFile("arca_type arclet\n"));
  metadata.set(arcletNameUrl, textFile("conversant-crane\n"));
  await renderMenu("/workspace", "remote", true);
  await renderMenu("src/main.ts", "remote");
  await waitFor(() =>
    expect(screen.getAllByRole("button", { name: "Open in VS Code" })).toHaveLength(2),
  );
  const calls = fetchHost.mock.calls.length;
  screen
    .getAllByRole("button", { name: "Open in VS Code" })
    .forEach((button) => fireEvent.click(button));
  expect(open.mock.calls.map(([url]) => url)).toEqual([
    "vscode://vscode-remote/ssh-remote+arclet_conversant-crane.ssh/workspace",
    "vscode://vscode-remote/ssh-remote+arclet_conversant-crane.ssh/workspace/src/main.ts:1:1",
  ]);
  expect(fetchHost.mock.calls.map(([input]) => input)).toEqual([
    "/v1/hosts",
    labelsUrl,
    arcletNameUrl,
  ]);
  expect(fetchHost).toHaveBeenCalledTimes(calls);
});

it.each([
  ["missing", 404],
  ["permission denied", 403],
  ["offline transport", 503],
  ["binary", { ...textFile("arca_type arca\n"), encoding: "base64" }],
  ["truncated", { ...textFile("arca_type arca\n"), truncated: true }],
  ["directory", { object: "list", data: [] }],
  ["invalid content", { ...textFile(""), content: null }],
  ["other machine type", textFile("arca_type isaac-arca\n")],
] as const)("hides remote Open when metadata is %s", async (_case, response) => {
  serverHosts = [host("remote", "alex.dev's arca")];
  metadata.set(labelsUrl, response);
  await renderMenu("/workspace", "remote", true);
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  expect(fetchHost.mock.calls.map(([input]) => input)).toEqual(["/v1/hosts", labelsUrl]);
});

it.each([404, textFile(""), textFile("arclet_duplicate"), textFile("two\nnames")])(
  "hides user-connected Arclets without a valid canonical name: %j",
  async (name) => {
    serverHosts = [host("remote", "valid-display-name")];
    metadata.set(labelsUrl, textFile("arca_type arclet\n"));
    metadata.set(arcletNameUrl, name);
    await renderMenu("/workspace", "remote", true);
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
    expect(fetchHost.mock.calls.map(([input]) => input)).toEqual([
      "/v1/hosts",
      labelsUrl,
      arcletNameUrl,
    ]);
  },
);

it("gates cached discovery on host availability and keeps it scoped to the session and host", async () => {
  serverHosts = [host("remote", "custom name"), host("other", "another host")];
  metadata.set(labelsUrl, textFile("arca_type arca\n"));
  const { changeTarget } = await renderMenu("/workspace", "remote", true);
  expect(await screen.findByRole("button", { name: "Open in VS Code" })).toBeVisible();
  serverHosts = [{ ...serverHosts[0], status: "offline" }, serverHosts[1]];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull());
  serverHosts = [host("remote", "renamed"), serverHosts[1]];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  expect(await screen.findByRole("button", { name: "Open in VS Code" })).toBeVisible();
  expect(fetchHost.mock.calls.filter(([input]) => input === labelsUrl)).toHaveLength(1);
  changeTarget("/workspace", "remote", true, "conv_2");
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  metadata.set(labelsUrl, 404);
  changeTarget("/workspace", "other");
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  expect(fetchHost.mock.calls.filter(([input]) => input === labelsUrl)).toHaveLength(2);
});

it("does not discover an offline host until it comes online", async () => {
  serverHosts = [{ ...host("remote", "custom name"), status: "offline" }];
  metadata.set(labelsUrl, textFile("arca_type arca\n"));
  await renderMenu("/workspace", "remote", true);
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(fetchHost.mock.calls.map(([input]) => input)).toEqual(["/v1/hosts"]);
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  serverHosts = [host("remote", "custom name")];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  expect(await screen.findByRole("button", { name: "Open in VS Code" })).toBeVisible();
});

it("ignores late discovery after switching sessions and clears a failed refresh", async () => {
  serverHosts = [host("remote", "custom name")];
  let finishRead!: (response: Response) => void;
  const fetchResponse = fetchHost.getMockImplementation()!;
  fetchHost.mockImplementation((input: string) =>
    input === labelsUrl
      ? new Promise<Response>((resolve) => {
          finishRead = resolve;
        })
      : fetchResponse(input),
  );
  const { changeTarget } = await renderMenu("/workspace", "remote", true);
  await waitFor(() => expect(finishRead).toBeDefined());
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  changeTarget("/workspace", "remote", true, "conv_2");
  await act(async () => finishRead(new Response(JSON.stringify(textFile("arca_type arca\n")))));
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  fetchHost.mockImplementation(fetchResponse);
  metadata.set(labelsUrl, textFile("arca_type arca\n"));
  changeTarget("/workspace", "remote");
  expect(await screen.findByRole("button", { name: "Open in VS Code" })).toBeVisible();
  metadata.set(labelsUrl, 403);
  await act(() => client.invalidateQueries({ queryKey: ["hostSshTarget"] }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull());
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
    if (hostId === "remote") {
      serverHosts = [host("remote", "conversant-crane", "arclet")];
    }
    const { setSessionHost, setSessionParent } = await import("@/lib/sessionHost");
    setSessionParent("conv_1", "parent");
    await renderMenu("/workspace", null, true);
    await renderMenu("src/main.ts", null);
    expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
    await act(() => setSessionHost("parent", hostId));
    const buttons = await screen.findAllByRole("button", { name: "Open in VS Code" });
    expect(buttons).toHaveLength(2);
    buttons.forEach((button) => fireEvent.click(button));
    const location =
      hostId === "local" ? "file" : "vscode-remote/ssh-remote+arclet_conversant-crane.ssh";
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

it.each([
  ["missing host", []],
  ["ordinary remote", [host("remote", "custom-alias")]],
  ["unsupported sandbox", [host("remote", "lakebox", "databricks-lakebox")]],
  ["invalid Arclet name", [host("remote", "bad name", "arclet")]],
] as const)("hides an unknown remote destination: %s", async (_case, hosts) => {
  serverHosts = [...hosts];
  await renderMenu("/workspace", "remote", true);
  await waitFor(() => expect(client.isFetching()).toBe(0));
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
});

it("updates and clears an inferred target when the host list changes", async () => {
  serverHosts = [host("remote", "first-bird", "arclet")];
  await renderMenu("/workspace", "remote", true);
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toContain("ssh-remote+arclet_first-bird.ssh");

  serverHosts = [{ ...host("remote", "renamed-bird", "arclet"), status: "offline" }];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull());
  serverHosts = [host("remote", "renamed-bird", "arclet")];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toContain("ssh-remote+arclet_renamed-bird.ssh");

  serverHosts = [];
  await act(() => client.invalidateQueries({ queryKey: ["hosts"] }));
  await waitFor(() => expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull());
});

it("uses the selected host row when switching between remote, unknown, and local hosts", async () => {
  serverHosts = [
    host("first", "first-bird", "arclet"),
    host("second", "my-arca", "databricks-arca"),
  ];
  const { changeTarget } = await renderMenu("/workspace", "first", true);
  fireEvent.click(await screen.findByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toContain("ssh-remote+arclet_first-bird.ssh");
  changeTarget("/second", "second");
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toBe("vscode://vscode-remote/ssh-remote+arca.ssh/second");
  changeTarget("/unknown", "unknown");
  expect(screen.queryByRole("button", { name: "Open in VS Code" })).toBeNull();
  changeTarget("/local", "local");
  fireEvent.click(screen.getByRole("button", { name: "Open in VS Code" }));
  expect(open.mock.calls.at(-1)?.[0]).toBe("vscode://file/local");
  expect(fetchHost.mock.calls.map(([input]) => input)).toEqual(["/v1/hosts"]);
});

it.each(["ios", "android"])("hides desktop editor choices in the %s shell", async (kind) => {
  vi.stubGlobal("omnigentDesktop", undefined);
  vi.stubGlobal("omnigentNative", { kind });
  await renderMenu("/workspace", "remote", true);
  expect(screen.queryByRole("button", { name: "Choose app to open in" })).toBeNull();
  expect(fetchHost).not.toHaveBeenCalled();
});
