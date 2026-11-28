import { describe, expect, it } from "vitest";

import { autoMergeKnown } from "./pullRequestAutoMerge";

describe("autoMergeKnown", () => {
  it("is on for GitHub's auto-merge request, and off where the read says there is none", () => {
    expect(
      autoMergeKnown({
        auto_merge: {
          merge_method: "SQUASH",
          enabled_by: "amy",
          enabled_at: "2026-11-05T10:00:00Z",
        },
      }),
    ).toBe("on");
    expect(autoMergeKnown({ auto_merge: null })).toBe("off");
  });

  it("can't say for a read without the field, or with one it can't read", () => {
    expect(autoMergeKnown(undefined)).toBe("unknown");
    expect(autoMergeKnown(null)).toBe("unknown");
    // An older shell's or host's read leaves it out, which isn't off.
    expect(autoMergeKnown({})).toBe("unknown");
    expect(autoMergeKnown({ auto_merge: undefined })).toBe("unknown");
    for (const garbled of ["on", 1, true, []]) {
      expect(autoMergeKnown({ auto_merge: garbled } as never)).toBe("unknown");
    }
  });
});
