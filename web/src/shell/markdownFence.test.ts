import { describe, expect, it } from "vitest";
import { fenceForBody } from "./markdownFence";

describe("fenceForBody", () => {
  it("uses three backticks for backtick-free content", () => {
    expect(fenceForBody("graph TD\nA-->B")).toBe("```");
    expect(fenceForBody("")).toBe("```");
  });

  it("uses one more backtick than the longest run in the body", () => {
    expect(fenceForBody("a ` b")).toBe("```"); // run of 1 → still min 3
    expect(fenceForBody("```")).toBe("````"); // run of 3 → 4
    expect(fenceForBody("````")).toBe("`````"); // run of 4 → 5
  });

  it("counts the longest run, not the total number of backticks", () => {
    expect(fenceForBody("` `` `")).toBe("```"); // longest run is 2 → 3
  });
});
