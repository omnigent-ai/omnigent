import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FileContextMenu, FileMenuProvider, resolveFileMenuPath } from "./FileContextMenu";

const mocks = vi.hoisted(() => ({
  copy: vi.fn(),
  reveal: vi.fn(),
  identity: vi.fn(),
  supported: vi.fn(),
  mac: vi.fn(),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: mocks.copy }));
vi.mock("@/lib/nativeBridge", () => ({
  supportsFileReveal: mocks.supported,
  isMacElectronShell: mocks.mac,
  getHostIdentity: mocks.identity,
  revealFile: mocks.reveal,
}));
afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
  mocks.supported.mockReturnValue(true);
  mocks.mac.mockReturnValue(true);
  mocks.identity.mockResolvedValue({ hostId: "local" });
  mocks.copy.mockResolvedValue(undefined);
  mocks.reveal.mockResolvedValue(true);
});

function openMenu(
  hostId = "local",
  deleted = false,
  root = "/Users/test/repo/src",
  path = "nested/file.txt",
) {
  const view = render(
    <FileMenuProvider root={root} hostId={hostId}>
      <FileContextMenu path={path} deleted={deleted}>
        <button type="button">File</button>
      </FileContextMenu>
    </FileMenuProvider>,
  );
  fireEvent.contextMenu(screen.getByText("File"));
  return view;
}

describe("file context menu", () => {
  it("copies the absolute path from the current browse location", async () => {
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Copy Path" }));
    await waitFor(() =>
      expect(mocks.copy).toHaveBeenCalledWith("/Users/test/repo/src/nested/file.txt"),
    );
  });

  it("copies the row's relative path", async () => {
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: "Copy Relative Path" }));
    await waitFor(() => expect(mocks.copy).toHaveBeenCalledWith("nested/file.txt"));
  });

  it("reveals a local item at the filesystem root", async () => {
    openMenu("local", false, "/");
    fireEvent.click(await screen.findByRole("menuitem", { name: "Show in Finder" }));
    expect(mocks.reveal).toHaveBeenCalledWith("local", "/nested/file.txt");
  });

  it.each([
    ["remote", false],
    ["local", true],
  ] as const)("does not reveal unavailable items (%s, deleted=%s)", async (hostId, deleted) => {
    openMenu(hostId, deleted);
    await waitFor(() => expect(mocks.identity).toHaveBeenCalled());
    expect(screen.queryByRole("menuitem", { name: "Show in Finder" })).toBeNull();
  });
});

describe("file menu paths", () => {
  it.each([
    ["/", "nested/file.txt", "/nested/file.txt"],
    ["/home/user/repo/", "src/file.txt", "/home/user/repo/src/file.txt"],
    ["C:\\", "src/file.txt", "C:\\src\\file.txt"],
    ["C:/Users/test", "src/file.txt", "C:\\Users\\test\\src\\file.txt"],
    ["\\\\server\\share", "dir/file.txt", "\\\\server\\share\\dir\\file.txt"],
  ])("resolves %s + %s", (root, relative, expected) => {
    expect(resolveFileMenuPath(root, relative)).toBe(expected);
  });
  it("does not invent an absolute path before environment metadata loads", () => {
    expect(resolveFileMenuPath(null, "file.txt")).toBeNull();
  });
});

it("keeps copying available in browsers and older desktop shells", () => {
  mocks.supported.mockReturnValue(false);
  openMenu();
  expect(screen.queryByRole("menuitem", { name: /Show in/ })).toBeNull();
  expect(mocks.identity).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("menuitem", { name: "Copy Path" }));
  expect(mocks.copy).toHaveBeenCalledWith("/Users/test/repo/src/nested/file.txt");
});

it.each([
  ["Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Show in File Explorer"],
  ["Mozilla/5.0 (X11; Linux x86_64)", "Show in File Manager"],
])("uses the platform's file manager label (%s)", async (userAgent, label) => {
  mocks.mac.mockReturnValue(false);
  const agent = vi.spyOn(navigator, "userAgent", "get").mockReturnValue(userAgent);
  try {
    openMenu();
    expect(await screen.findByRole("menuitem", { name: label })).toBeInTheDocument();
  } finally {
    agent.mockRestore();
  }
});

it("updates the absolute path after navigating to another directory", () => {
  const view = openMenu();
  view.rerender(
    <FileMenuProvider root="/tmp/other" hostId="local">
      <FileContextMenu path="nested/file.txt">
        <button type="button">File</button>
      </FileContextMenu>
    </FileMenuProvider>,
  );
  fireEvent.click(screen.getByRole("menuitem", { name: "Copy Path" }));
  expect(mocks.copy).toHaveBeenCalledWith("/tmp/other/nested/file.txt");
});

it("reveals absolute viewer paths outside the workspace without prefixing the root", async () => {
  openMenu("local", false, "/workspace", "/tmp/report.md");
  fireEvent.click(await screen.findByRole("menuitem", { name: "Show in Finder" }));
  expect(mocks.reveal).toHaveBeenCalledWith("local", "/tmp/report.md");
});

it("does not offer a misleading relative path for an outside-workspace file", () => {
  openMenu("remote", false, "/workspace", "/workspace-other/report.md");
  expect(screen.queryByRole("menuitem", { name: "Copy Relative Path" })).toBeNull();
  fireEvent.click(screen.getByRole("menuitem", { name: "Copy Path" }));
  expect(mocks.copy).toHaveBeenCalledWith("/workspace-other/report.md");
});

it("copies a workspace-relative path for an absolute viewer path inside it", () => {
  openMenu("remote", false, "/workspace", "/workspace/docs/report.md");
  fireEvent.click(screen.getByRole("menuitem", { name: "Copy Relative Path" }));
  expect(mocks.copy).toHaveBeenCalledWith("docs/report.md");
});
