import { afterEach, expect, it, vi } from "vitest";
import { editorLink, launchEditorLink, normalizeSshTarget } from "./editorLinks";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it.each([
  ["/repo", null, "vscode://file/repo"],
  [
    "/repo/a #?%.ts",
    "dev-box",
    "vscode://vscode-remote/ssh-remote+dev-box/repo/a%20%23%3F%25.ts:1:1",
  ],
  [
    "/repo/日本語.md",
    "workspace.example.test",
    "vscode://vscode-remote/ssh-remote+workspace.example.test/repo/%E6%97%A5%E6%9C%AC%E8%AA%9E.md:1:1",
  ],
  ["/repo", "me@dev", "vscode://vscode-remote/ssh-remote+me%40dev/repo:1:1"],
  ["C:\\Users\\me\\repo", null, "vscode://file/C%3A/Users/me/repo"],
])("encodes %s on %s", (path, target, expected) => {
  expect(editorLink("vscode", path!, target)).toBe(expected);
  expect(editorLink("cursor", path!, target)).toBe(expected!.replace(/^vscode:/, "cursor:"));
});

it("keeps remote folder links distinct from file links", () => {
  expect(editorLink("cursor", "/repo", "dev-box", true)).toBe(
    "cursor://vscode-remote/ssh-remote+dev-box/repo",
  );
  expect(editorLink("cursor", "/repo/file.ts", "dev-box", false)).toBe(
    "cursor://vscode-remote/ssh-remote+dev-box/repo/file.ts:1:1",
  );
});

it.each(["relative/file", "/file\0", "/file\n", ""])("rejects invalid path %j", (path) => {
  expect(editorLink("cursor", path, null)).toBeNull();
});

it.each([
  null,
  undefined,
  "",
  "bad target",
  "box\n",
  "box/path",
  "box?x",
  "-box",
  "@dev",
  "me@",
  "me@@dev",
  "me@box@dev",
  1,
  [],
  "x".repeat(256),
])("rejects invalid target %j", (target) => expect(normalizeSshTarget(target)).toBeNull());

it("does not turn an invalid remote target into a local link", () => {
  expect(editorLink("cursor", "/repo", "")).toBeNull();
  expect(editorLink("cursor", "/repo", "bad target")).toBeNull();
});

it("uses the existing Electron protocol handler", () => {
  vi.stubGlobal("omnigentDesktop", { kind: "electron" });
  const open = vi.fn();
  vi.stubGlobal("open", open);
  launchEditorLink("cursor://vscode-remote/ssh-remote+dev-box/repo");
  expect(open).toHaveBeenCalledWith(
    "cursor://vscode-remote/ssh-remote+dev-box/repo",
    "_blank",
    "noopener,noreferrer",
  );
  expect(document.querySelector("iframe")).toBeNull();
});

it("hands browser links off synchronously through a same-context anchor, avoiding frame-src CSP", () => {
  vi.stubGlobal("omnigentDesktop", undefined);
  const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
    this: HTMLAnchorElement,
  ) {
    expect(this.isConnected).toBe(true);
    expect(this.href).toBe("vscode://vscode-remote/ssh-remote+dev-box/repo");
    expect(this.target).toBe("_self");
    expect(this.rel).toBe("noreferrer");
  });
  launchEditorLink("vscode://vscode-remote/ssh-remote+dev-box/repo");
  expect(click).toHaveBeenCalledOnce();
  expect(document.querySelector('a[href^="vscode:"]')).toBeNull();
  expect(document.querySelector("iframe")).toBeNull();
});
