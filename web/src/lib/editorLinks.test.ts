import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_CODE_EDITOR_STORAGE_KEY,
  defaultEditorLink,
  launchEditorLink,
  readDefaultCodeEditor,
  usableEditorLinks,
  writeDefaultCodeEditor,
} from "./editorLinks";
import type { EditorLink } from "./types";

const link = (editor: string, local_only = false): EditorLink => ({
  editor,
  label: editor,
  url: `${editor}://file/w`,
  local_only,
});

afterEach(() => {
  localStorage.clear();
  document.querySelectorAll("iframe").forEach((frame) => frame.remove());
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("usableEditorLinks", () => {
  it("keeps remote links everywhere", () => {
    expect(usableEditorLinks([link("cursor")], "host_1", null)).toEqual([link("cursor")]);
  });

  it("keeps local-only links only when the session runs on this machine", () => {
    const links = [link("cursor", true)];
    expect(usableEditorLinks(links, "host_1", "host_1")).toEqual(links);
    expect(usableEditorLinks(links, "host_1", "host_2")).toEqual([]);
    expect(usableEditorLinks(links, "host_1", null)).toEqual([]);
    expect(usableEditorLinks(links, null, null)).toEqual([]);
  });

  it("treats missing links as none", () => {
    expect(usableEditorLinks(undefined, "host_1", "host_1")).toEqual([]);
    expect(usableEditorLinks(undefined, "host_1", "host_1")).toEqual([]);
  });
});

describe("defaultEditorLink", () => {
  const links = [link("cursor"), link("vscode")];

  it("uses the remembered editor when offered, else the server's first", () => {
    expect(defaultEditorLink(links, "vscode")).toBe(links[1]);
    expect(defaultEditorLink(links, "zed")).toBe(links[0]);
    expect(defaultEditorLink(links, null)).toBe(links[0]);
    expect(defaultEditorLink([], "cursor")).toBeNull();
  });
});

describe("default editor preference", () => {
  it("round-trips through localStorage", () => {
    expect(readDefaultCodeEditor()).toBeNull();
    writeDefaultCodeEditor("vscode");
    expect(localStorage.getItem(DEFAULT_CODE_EDITOR_STORAGE_KEY)).toBe("vscode");
    expect(readDefaultCodeEditor()).toBe("vscode");
  });
});

describe("launchEditorLink", () => {
  it("loads the link in a hidden iframe in a browser", () => {
    launchEditorLink("cursor://file/w");
    const frames = [...document.querySelectorAll("iframe")];
    expect(frames.map((frame) => frame.getAttribute("src"))).toEqual(["cursor://file/w"]);
    expect(frames[0].style.display).toBe("none");
  });
});
