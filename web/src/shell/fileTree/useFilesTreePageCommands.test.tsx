import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/useSession", () => ({
  useSession: () => ({ session: null, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceEnvironment: () => ({ data: { root: "/workspace" } }),
}));

import type * as WorkspaceChangedFilesModule from "@/hooks/useWorkspaceChangedFiles";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { rememberBrowseLocation } from "../filesPane/browseLocations";
import {
  HIDDEN_FILE_REASON,
  REVEAL_ACTIVE_FILE_PALETTE_LABEL,
  forgetActiveFileRevealsForTests,
  useActiveFileRevealRequests,
} from "./revealActiveFile";
import {
  ACTIVE_FOLDER_AS_ROOT_LABEL,
  BACK_TO_WORKING_FOLDER_LABEL,
  useFilesTreeShowRequests,
} from "./treeRoot";
import { useFilesTreePageCommands } from "./useFilesTreePageCommands";

const revealCommands = () =>
  Object.values(usePaletteCommandsStore.getState().groups)
    .flatMap((group) => group.commands)
    .filter((command) => command.label === REVEAL_ACTIVE_FILE_PALETTE_LABEL);

afterEach(() => forgetActiveFileRevealsForTests());

describe("useFilesTreePageCommands", () => {
  it("asks its page to show the tree, then its panel to reveal the file", () => {
    const showTree = vi.fn();
    renderHook(() => useFilesTreeShowRequests("conv_place_reveal", showTree));
    const props = {
      conversationId: "conv_place_reveal",
      activePath: "src/App.tsx",
      showHidden: false,
      enabled: true,
    };
    const view = renderHook(() => useFilesTreePageCommands(props));
    const [command] = revealCommands();
    expect(command.availability).toEqual({ available: true });

    act(() => command.run());
    expect(showTree).toHaveBeenCalledTimes(1);
    // The panel the tree mounts takes up the request asked for before it existed.
    const taken = vi.fn();
    renderHook(() => useActiveFileRevealRequests("conv_place_reveal", taken));
    expect(taken).toHaveBeenCalledTimes(1);
    view.unmount();
    expect(revealCommands()).toHaveLength(0);
  });

  it("offers nothing while its page is off screen", () => {
    renderHook(() =>
      useFilesTreePageCommands({
        conversationId: "conv_place_hidden",
        activePath: "src/App.tsx",
        showHidden: false,
        enabled: false,
      }),
    );
    expect(revealCommands()).toHaveLength(0);
  });

  it("follows the folder the tree browses, with the same file open", () => {
    const conversationId = "conv_place_browse";
    renderHook(() =>
      useFilesTreePageCommands({
        conversationId,
        activePath: ".github/workflows/ci.yml",
        showHidden: false,
        enabled: true,
      }),
    );
    expect(revealCommands()[0].availability).toEqual({
      available: false,
      reason: HIDDEN_FILE_REASON,
    });

    // Inside .github, the file's path in the tree has no hidden part.
    act(() => rememberBrowseLocation(conversationId, "/workspace/.github"));
    expect(revealCommands()[0].availability).toEqual({ available: true });

    act(() => rememberBrowseLocation(conversationId, null));
    expect(revealCommands()[0].availability).toMatchObject({ available: false });
  });

  it("roots the tree and returns it from ⌘K, asking for a hidden tree each time", () => {
    const conversationId = "conv_place_root";
    const showTree = vi.fn();
    renderHook(() => useFilesTreeShowRequests(conversationId, showTree));
    renderHook(() =>
      useFilesTreePageCommands({
        conversationId,
        activePath: "experimental/iulian.dragos/OWNERS",
        showHidden: false,
        enabled: true,
      }),
    );
    const command = (label: string) =>
      Object.values(usePaletteCommandsStore.getState().groups)
        .flatMap((group) => group.commands)
        .find((candidate) => candidate.label === label)!;
    expect(command(BACK_TO_WORKING_FOLDER_LABEL).availability).toMatchObject({ available: false });

    act(() => command(ACTIVE_FOLDER_AS_ROOT_LABEL).run());
    expect(showTree).toHaveBeenCalledTimes(1);
    expect(command(ACTIVE_FOLDER_AS_ROOT_LABEL).availability).toMatchObject({ available: false });
    expect(command(BACK_TO_WORKING_FOLDER_LABEL).availability).toEqual({ available: true });

    act(() => command(BACK_TO_WORKING_FOLDER_LABEL).run());
    expect(showTree).toHaveBeenCalledTimes(2);
    expect(command(ACTIVE_FOLDER_AS_ROOT_LABEL).availability).toEqual({ available: true });
  });
});
