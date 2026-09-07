import { cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useNewSessionTarget } from "./useNewSessionTarget";
import {
  NEW_SESSION_TARGET_STORAGE_KEY,
  readNewSessionTarget,
  writeNewSessionTarget,
} from "@/lib/newSessionTarget";

beforeEach(() => localStorage.clear());
afterEach(cleanup);

describe("useNewSessionTarget", () => {
  it("updates a renamed project and clears a deleted target for every subscriber", async () => {
    writeNewSessionTarget({ kind: "project", projectId: "prj_alpha", projectName: "Old name" });
    const { result, rerender } = renderHook(({ projects }) => useNewSessionTarget(projects), {
      initialProps: { projects: [{ id: "prj_alpha", name: "New name" }] },
    });

    await waitFor(() =>
      expect(readNewSessionTarget()).toEqual({
        kind: "project",
        projectId: "prj_alpha",
        projectName: "New name",
      }),
    );
    expect(result.current.route).toBe("/?project=New%20name");

    rerender({ projects: [] });

    await waitFor(() => expect(result.current.target).toEqual({ kind: "none" }));
    expect(localStorage.getItem(NEW_SESSION_TARGET_STORAGE_KEY)).toBeNull();
  });
});
