import { afterEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  copyText: vi.fn(() => Promise.resolve()),
  toast: { success: vi.fn(), error: vi.fn() },
  openLink: vi.fn(),
  followLinkWithPopupFallback: vi.fn(),
  requestFileAction: vi.fn(),
}));
vi.mock("@/lib/clipboard", () => ({ copyText: mocks.copyText }));
vi.mock("sonner", () => ({ toast: mocks.toast }));
vi.mock("./fileActionStore", () => ({ requestFileAction: mocks.requestFileAction }));
vi.mock("@/lib/openLinkInApp", () => ({
  openLink: mocks.openLink,
  followLinkWithPopupFallback: mocks.followLinkWithPopupFallback,
}));

import {
  REPOSITORY_FILE_ENVIRONMENT,
  reviewFileActions,
  reviewFolderActions,
} from "./reviewFileActions";
import { resolveFileCommandTarget } from "./target";

const target = resolveFileCommandTarget("src/new.ts", {
  root: null,
  hostId: null,
  conversationId: "conv_1",
  repositoryFile: true,
});
const URL = "https://example.com/fork/app/blob/head/src/new.ts";

afterEach(() => vi.clearAllMocks());

describe("reviewFileActions", () => {
  it("copies a rename's previous path, and offers it only for a rename", async () => {
    const actions = reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT, {
      previousPath: "src/old.ts",
    });
    const copy = actions.find((action) => action.id === "copy-previous-path")!;
    expect(copy.label).toBe("Copy previous path");
    copy.run();
    expect(mocks.copyText).toHaveBeenCalledWith("src/old.ts");
    await vi.waitFor(() =>
      expect(mocks.toast.success).toHaveBeenCalledWith("Copied previous path"),
    );
    expect(
      reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT).map((action) => action.id),
    ).not.toContain("copy-previous-path");
  });

  it("makes the code host action a link, and ⌘K's run opens the same address", () => {
    const [open] = reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT, {
      codeHostLink: { label: "Open in GitHub", url: URL },
    });
    expect(open).toMatchObject({ id: "open-on-code-host", link: { href: URL } });
    const click = new MouseEvent("click");
    open!.link!.follow(click);
    expect(mocks.followLinkWithPopupFallback).toHaveBeenCalledWith(click, URL, "conv_1");
    open!.run();
    expect(mocks.openLink).toHaveBeenCalledWith("conv_1", URL);
  });

  it("lists a link that can't open as disabled, with its reason and no address", () => {
    const [open] = reviewFileActions(target, REPOSITORY_FILE_ENVIRONMENT, {
      codeHostLink: {
        label: "Open in GitHub",
        url: null,
        unavailableReason: "Deleted files can't be opened in GitHub.",
      },
    });
    expect(open).toMatchObject({
      id: "open-on-code-host",
      disabledReason: "Deleted files can't be opened in GitHub.",
      link: undefined,
    });
    open!.run();
    expect(mocks.openLink).not.toHaveBeenCalled();
  });
});

describe("reviewFolderActions", () => {
  const folder = resolveFileCommandTarget("web/src/shell", {
    root: null,
    hostId: null,
    conversationId: "conv_1",
    repositoryFile: true,
  });

  it("offers a pull request folder only its path to copy, copied as a folder", () => {
    const actions = reviewFolderActions(folder, REPOSITORY_FILE_ENVIRONMENT);
    expect(actions.map((action) => [action.id, action.label])).toEqual([
      ["copy-path", "Copy path"],
    ]);
    actions[0]!.run();
    expect(mocks.requestFileAction).toHaveBeenCalledWith(
      expect.objectContaining({ relativePath: "web/src/shell", directory: true }),
      "copy-path",
    );
  });

  it("offers a workspace folder its absolute and relative paths", () => {
    const local = resolveFileCommandTarget("web/src", {
      root: "/work/app",
      hostId: null,
      conversationId: "conv_1",
    });
    const environment = { ...REPOSITORY_FILE_ENVIRONMENT, local: true };
    expect(reviewFolderActions(local, environment).map((action) => action.id)).toEqual([
      "copy-path",
      "copy-relative-path",
    ]);
  });
});
