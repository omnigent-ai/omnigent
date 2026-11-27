import { describe, expect, it } from "vitest";
import type { WorkspaceReach } from "@/hooks/useWorkspaceChangedFiles";
import { browseNavigation } from "./BrowseLocationBar";

const workspace = "/home/user/project";
const roamingReach: WorkspaceReach = { unconfined: true, roots: [] };

describe("browseNavigation", () => {
  it("steps up inside the workspace and offers the way back", () => {
    expect(
      browseNavigation({
        current: "/home/user/project/src/lib",
        workspace,
        hostId: null,
        canBrowseOutside: false,
        reach: null,
      }),
    ).toEqual({ canRoam: false, parent: "/home/user/project/src", workspaceRoot: workspace });
  });

  it("stops at the workspace root for a viewer who can't roam", () => {
    expect(
      browseNavigation({
        current: workspace,
        workspace,
        hostId: "host_1",
        canBrowseOutside: false,
        reach: roamingReach,
      }),
    ).toEqual({ canRoam: false, parent: null, workspaceRoot: null });
  });

  it("steps above the workspace for an owner on a host that reaches outside it", () => {
    expect(
      browseNavigation({
        current: workspace,
        workspace,
        hostId: "host_1",
        canBrowseOutside: true,
        reach: roamingReach,
      }),
    ).toEqual({ canRoam: true, parent: "/home/user", workspaceRoot: null });
  });

  it("knows the workspace root however its path is spelled", () => {
    const at = (current: string, root: string) =>
      browseNavigation({
        current,
        workspace: root,
        hostId: null,
        canBrowseOutside: false,
        reach: null,
      }).workspaceRoot;
    expect(at("/home/user/project/", workspace)).toBeNull();
    expect(at("C:\\work\\project", "C:/work/project")).toBeNull();
    expect(at("/home/user/project/src", workspace)).toBe(workspace);
  });

  it("offers only the way back to a viewer who can't roam, from a folder outside the workspace", () => {
    expect(
      browseNavigation({
        current: "/home/user/elsewhere",
        workspace,
        hostId: "host_1",
        canBrowseOutside: false,
        reach: roamingReach,
      }),
    ).toEqual({ canRoam: false, parent: null, workspaceRoot: workspace });
  });

  it("doesn't roam for an owner before the session says what it can reach", () => {
    expect(
      browseNavigation({
        current: workspace,
        workspace,
        hostId: "host_1",
        canBrowseOutside: true,
        reach: null,
      }),
    ).toEqual({ canRoam: false, parent: null, workspaceRoot: null });
  });

  it("doesn't roam without a host to browse", () => {
    expect(
      browseNavigation({
        current: workspace,
        workspace,
        hostId: null,
        canBrowseOutside: true,
        reach: roamingReach,
      }).canRoam,
    ).toBe(false);
  });
});
