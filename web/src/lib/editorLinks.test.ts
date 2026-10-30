import { afterEach, expect, it, vi } from "vitest";
import {
  editorLink,
  inferHostSshTarget,
  launchEditorLink,
  normalizeSshTarget,
} from "./editorLinks";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.querySelectorAll("iframe").forEach((frame) => frame.remove());
});

it.each([
  ["/repo", null, "vscode://file/repo"],
  [
    "/repo/a #?%.ts",
    "arclet_dev.ssh",
    "vscode://vscode-remote/ssh-remote+arclet_dev.ssh/repo/a%20%23%3F%25.ts:1:1",
  ],
  [
    "/repo/日本語.md",
    "arca.ssh",
    "vscode://vscode-remote/ssh-remote+arca.ssh/repo/%E6%97%A5%E6%9C%AC%E8%AA%9E.md:1:1",
  ],
  ["/repo", "me@dev", "vscode://vscode-remote/ssh-remote+me%40dev/repo:1:1"],
  ["C:\\Users\\me\\repo", null, "vscode://file/C%3A/Users/me/repo"],
])("encodes %s on %s", (path, target, expected) => {
  expect(editorLink("vscode", path!, target)).toBe(expected);
  expect(editorLink("cursor", path!, target)).toBe(expected!.replace(/^vscode:/, "cursor:"));
});

it("keeps remote folder links distinct from file links", () => {
  expect(editorLink("cursor", "/repo", "arclet_dev.ssh", true)).toBe(
    "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/repo",
  );
  expect(editorLink("cursor", "/repo/file.ts", "arclet_dev.ssh", false)).toBe(
    "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/repo/file.ts:1:1",
  );
});

it.each([
  [{ name: "my-arca", sandbox_provider: "databricks-arca" }, "arca.ssh"],
  [{ name: "alex.dev's arca", sandbox_provider: null }, "arca.ssh"],
  [{ name: "alex-dev_2's arca" }, "arca.ssh"],
  [{ name: "conversant-crane", sandbox_provider: "arclet" }, "arclet_conversant-crane.ssh"],
  [
    { name: "omnigent-arclet-session", sandbox_provider: "arclet" },
    "arclet_omnigent-arclet-session.ssh",
  ],
  [{ name: "x".repeat(64), sandbox_provider: "arclet" }, `arclet_${"x".repeat(64)}.ssh`],
] as const)("infers the standard Databricks SSH target from %j", (host, expected) => {
  expect(inferHostSshTarget(host)).toBe(expected);
});

it.each([
  undefined,
  null,
  { name: "remote", sandbox_provider: null },
  { name: "alex.dev's laptop", sandbox_provider: null },
  { name: "'s arca", sandbox_provider: null },
  { name: " alex.dev's arca", sandbox_provider: null },
  { name: "alex.dev's arca\n", sandbox_provider: null },
  { name: "alex.dev's arca backup", sandbox_provider: null },
  { name: "alex/dev's arca", sandbox_provider: null },
  { name: "alex.dev's arca", sandbox_provider: "" },
  { name: "alex.dev's arca", sandbox_provider: "arclet" },
  { name: "alex.dev's arca", sandbox_provider: "databricks-lakebox" },
  { name: "lakebox", sandbox_provider: "databricks-lakebox" },
  { name: "", sandbox_provider: "arclet" },
  { name: "bad name", sandbox_provider: "arclet" },
  { name: "bad.name", sandbox_provider: "arclet" },
  { name: "bird\n", sandbox_provider: "arclet" },
  { name: "bird/other", sandbox_provider: "arclet" },
  { name: "bird@other", sandbox_provider: "arclet" },
  { name: "bird?query", sandbox_provider: "arclet" },
  { name: "bird#fragment", sandbox_provider: "arclet" },
  { name: "-bad", sandbox_provider: "arclet" },
  { name: "arclet_dev", sandbox_provider: "arclet" },
  { name: "Arclet-dev", sandbox_provider: "arclet" },
  { name: "x".repeat(65), sandbox_provider: "arclet" },
])("does not infer an SSH target from %j", (host) => {
  expect(inferHostSshTarget(host)).toBeNull();
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
  launchEditorLink("cursor://vscode-remote/ssh-remote+arclet_dev.ssh/repo");
  expect(open).toHaveBeenCalledWith(
    "cursor://vscode-remote/ssh-remote+arclet_dev.ssh/repo",
    "_blank",
    "noopener,noreferrer",
  );
  expect(document.querySelector("iframe")).toBeNull();
});

it("hands browser links off synchronously without navigating the tab", () => {
  vi.stubGlobal("omnigentDesktop", undefined);
  vi.useFakeTimers();
  const before = location.href;
  launchEditorLink("vscode://vscode-remote/ssh-remote+arca.ssh/repo");
  const frame = document.querySelector("iframe");
  expect(frame).toHaveAttribute("src", "vscode://vscode-remote/ssh-remote+arca.ssh/repo");
  expect(frame?.hidden).toBe(true);
  expect(location.href).toBe(before);
  vi.advanceTimersByTime(1000);
  expect(document.querySelector("iframe")).toBeNull();
});
