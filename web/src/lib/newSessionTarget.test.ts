import { beforeEach, describe, expect, it } from "vitest";

import {
  NEW_SESSION_TARGET_STORAGE_KEY,
  newSessionRoute,
  parseNewSessionTarget,
  readNewSessionTarget,
  resolveNewSessionTarget,
  writeNewSessionTarget,
} from "./newSessionTarget";

beforeEach(() => localStorage.clear());

describe("newSessionTarget", () => {
  it("persists a first-class project identity and builds its composer route", () => {
    writeNewSessionTarget({ kind: "project", projectId: "prj_alpha", projectName: "Alpha Team" });

    const target = readNewSessionTarget();
    expect(target).toEqual({
      kind: "project",
      projectId: "prj_alpha",
      projectName: "Alpha Team",
    });
    expect(newSessionRoute(target)).toBe("/?project=Alpha%20Team");
  });

  it("resolves a rename by stable id and clears a deleted project", () => {
    const stored = { kind: "project", projectId: "prj_alpha", projectName: "Old name" } as const;

    expect(resolveNewSessionTarget(stored, [{ id: "prj_alpha", name: "New name" }])).toEqual({
      kind: "project",
      projectId: "prj_alpha",
      projectName: "New name",
    });
    expect(resolveNewSessionTarget(stored, [])).toEqual({ kind: "none" });
  });

  it("promotes a legacy name target once a first-class project appears", () => {
    expect(
      resolveNewSessionTarget({ kind: "project", projectId: null, projectName: "Legacy" }, [
        { id: "prj_legacy", name: "Legacy" },
      ]),
    ).toEqual({ kind: "project", projectId: "prj_legacy", projectName: "Legacy" });
  });

  it("falls back safely for corrupt storage and removes storage for No Project", () => {
    expect(parseNewSessionTarget("{broken")).toEqual({ kind: "none" });
    localStorage.setItem(NEW_SESSION_TARGET_STORAGE_KEY, "{broken");
    expect(readNewSessionTarget()).toEqual({ kind: "none" });

    writeNewSessionTarget({ kind: "none" });
    expect(localStorage.getItem(NEW_SESSION_TARGET_STORAGE_KEY)).toBeNull();
  });
});
