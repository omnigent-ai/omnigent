import { describe, expect, it } from "vitest";
import { AVAILABLE, HIDDEN } from "@/store/paletteCommands";
import { FILE_COMMAND_IDS, fileCommands, fileCommandsById, type FileCommandFacts } from "./model";

function facts(overrides: Partial<FileCommandFacts> = {}): FileCommandFacts {
  return {
    directory: false,
    deleted: false,
    relativePath: "src/main.ts",
    absolutePath: "/repo/src/main.ts",
    openable: true,
    newTabOpenable: true,
    rootable: true,
    linkable: true,
    revealable: true,
    fileManager: "Finder",
    editors: ["vscode", "cursor"],
    ...overrides,
  };
}

function availableIds(input: FileCommandFacts): string[] {
  return fileCommands(input)
    .filter((command) => command.availability.available)
    .map((command) => command.id);
}

describe("fileCommands", () => {
  it("lists every action once, in full-menu order", () => {
    expect(fileCommands(facts()).map((command) => command.id)).toEqual([...FILE_COMMAND_IDS]);
    expect(Object.keys(fileCommandsById(facts())).sort()).toEqual([...FILE_COMMAND_IDS].sort());
  });

  it("offers everything but the folder root for a local file the surface can open", () => {
    const codeHostLink = { label: "Open in GitHub", url: "https://github.com/a/b/blob/c/d" };
    expect(availableIds(facts({ codeHostLink, previousPath: "src/old.ts" }))).toEqual(
      FILE_COMMAND_IDS.filter((id) => id !== "open-as-root"),
    );
    const byId = fileCommandsById(facts());
    expect(byId.reveal.label).toBe("Show in Finder");
    expect(byId["open-in-cursor"].label).toBe("Open in Cursor");
  });

  it("names the code host action by its link, and hides it without one", () => {
    const linked = fileCommandsById(
      facts({ codeHostLink: { label: "Open in GitHub", url: "https://x/y" } }),
    )["open-on-code-host"];
    expect(linked.label).toBe("Open in GitHub");
    expect(linked.availability).toEqual(AVAILABLE);
    expect(linked.keywords).toEqual(expect.arrayContaining(["github"]));
    expect(fileCommandsById(facts())["open-on-code-host"].availability).toEqual(HIDDEN);
    expect(
      fileCommandsById(facts({ codeHostLink: null }))["open-on-code-host"].availability,
    ).toEqual(HIDDEN);
  });

  it("opens a folder in the file manager but never offers it to the viewer or a link", () => {
    const byId = fileCommandsById(facts({ directory: true }));
    expect(byId.reveal.label).toBe("Open in Finder");
    expect(byId.open.availability).toEqual(HIDDEN);
    expect(byId["open-in-new-tab"].availability).toEqual(HIDDEN);
    expect(byId["open-as-root"].availability).toEqual(AVAILABLE);
    expect(byId["copy-link"].availability).toEqual(HIDDEN);
    expect(byId["copy-path"].availability).toEqual(AVAILABLE);
  });

  it("only copies a deleted file's or folder's paths, and a file's link", () => {
    expect(availableIds(facts({ deleted: true }))).toEqual([
      "copy-path",
      "copy-relative-path",
      "copy-link",
    ]);
    expect(availableIds(facts({ deleted: true, directory: true }))).toEqual([
      "copy-path",
      "copy-relative-path",
    ]);
  });

  it("copies the workspace path as the path until the workspace location loads", () => {
    const byId = fileCommandsById(facts({ absolutePath: null, editors: [], revealable: false }));
    expect(byId["copy-path"].availability).toEqual(AVAILABLE);
    // Both rows would copy the same text.
    expect(byId["copy-relative-path"].availability).toEqual(HIDDEN);
  });

  it("drops the workspace-relative actions for a file outside the workspace", () => {
    expect(availableIds(facts({ relativePath: null }))).toEqual([
      "open",
      "open-in-new-tab",
      "copy-path",
      "reveal",
      "open-in-vscode",
      "open-in-cursor",
    ]);
  });

  it("has no relative path to copy for the workspace root itself", () => {
    const byId = fileCommandsById(facts({ relativePath: "", directory: true }));
    expect(byId["copy-relative-path"].availability).toEqual(HIDDEN);
  });

  it("hides what this machine or surface can't do", () => {
    expect(
      availableIds(
        facts({
          openable: false,
          newTabOpenable: false,
          rootable: false,
          linkable: false,
          revealable: false,
          editors: ["cursor"],
        }),
      ),
    ).toEqual(["copy-path", "copy-relative-path", "open-in-cursor"]);
  });
});
