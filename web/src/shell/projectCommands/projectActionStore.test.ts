import { afterEach, describe, expect, it } from "vitest";

import {
  consumeProjectRename,
  reportProjectRenamed,
  useProjectActionStore,
} from "./projectActionStore";

afterEach(() => {
  useProjectActionStore.setState({ request: null, lastRename: null });
});

describe("consumeProjectRename", () => {
  it("clears the rename it was handed", () => {
    reportProjectRenamed("Alpha", "Beta");
    consumeProjectRename(useProjectActionStore.getState().lastRename!.key);
    expect(useProjectActionStore.getState().lastRename).toBeNull();
  });

  it("keeps a newer rename reported in the meantime", () => {
    reportProjectRenamed("Alpha", "Beta");
    const handled = useProjectActionStore.getState().lastRename!.key;
    reportProjectRenamed("Beta", "Gamma");
    consumeProjectRename(handled);
    expect(useProjectActionStore.getState().lastRename).toMatchObject({
      oldName: "Beta",
      newName: "Gamma",
    });
  });
});
