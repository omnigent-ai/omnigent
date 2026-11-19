import { describe, expect, it, vi } from "vitest";

import { VIEW_FILE_ACTION_ID, viewFileAction } from "./viewFileAction";

describe("viewFileAction", () => {
  it("is View File, and opens its own file", () => {
    const open = vi.fn();
    const action = viewFileAction("src/app.ts", open);
    expect(action).toMatchObject({ id: VIEW_FILE_ACTION_ID, label: "View File" });
    action.run();
    expect(open).toHaveBeenCalledWith("src/app.ts");
  });
});
