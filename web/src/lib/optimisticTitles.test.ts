import { afterEach, describe, expect, it } from "vitest";
import {
  clearOptimisticTitles,
  getOptimisticTitle,
  recordOptimisticTitle,
  synthesizeOptimisticTitle,
} from "./optimisticTitles";

afterEach(() => {
  clearOptimisticTitles();
});

describe("synthesizeOptimisticTitle", () => {
  it("collapses whitespace and newlines into one line", () => {
    expect(synthesizeOptimisticTitle("  read the README\n\nand   refactor\tit ")).toBe(
      "read the README and refactor it",
    );
  });

  it("drops attachment-marker lines so paths never become the title", () => {
    expect(
      synthesizeOptimisticTitle(
        "[Attached: /tmp/notes.md]\n[Attached file: /tmp/x.png]\nsummarize",
      ),
    ).toBe("summarize");
    expect(synthesizeOptimisticTitle("[Attachment foo.png could not be loaded]\ndescribe it")).toBe(
      "describe it",
    );
  });

  it("returns null when no usable text remains", () => {
    expect(synthesizeOptimisticTitle("   \n\t ")).toBeNull();
    expect(synthesizeOptimisticTitle("[Attached: /tmp/only.md]")).toBeNull();
  });

  it("keeps titles at or under the limit untouched", () => {
    const exact = "x".repeat(60);
    expect(synthesizeOptimisticTitle(exact)).toBe(exact);
  });

  it("truncates beyond the limit with an ellipsis at 60 chars total", () => {
    const title = synthesizeOptimisticTitle("a".repeat(70));
    expect(title).toBe("a".repeat(59) + "…");
    expect([...(title ?? "")]).toHaveLength(60);
  });

  it("trims trailing whitespace before the ellipsis", () => {
    // 58 letters + 2 spaces + more text: the raw cut lands mid-gap, and the
    // server rstrips before appending the ellipsis.
    const title = synthesizeOptimisticTitle(`${"a".repeat(58)}  ${"b".repeat(10)}`);
    expect(title).toBe("a".repeat(58) + "…");
  });

  it("counts code points, not UTF-16 units, so emoji don't split", () => {
    const title = synthesizeOptimisticTitle("🚀".repeat(70));
    expect([...(title ?? "")]).toHaveLength(60);
    expect(title?.endsWith("…")).toBe(true);
  });
});

describe("recordOptimisticTitle", () => {
  it("stores the synthesized title under the conversation id", () => {
    recordOptimisticTitle("conv_1", "  fix the flaky test  ");
    expect(getOptimisticTitle("conv_1")).toBe("fix the flaky test");
  });

  it("records nothing when the prompt has no usable text", () => {
    recordOptimisticTitle("conv_2", "  \n ");
    recordOptimisticTitle("conv_3", "[Attached: /tmp/only.md]");
    expect(getOptimisticTitle("conv_2")).toBeUndefined();
    expect(getOptimisticTitle("conv_3")).toBeUndefined();
  });

  it("clears all entries at once", () => {
    recordOptimisticTitle("conv_1", "one");
    recordOptimisticTitle("conv_2", "two");
    clearOptimisticTitles();
    expect(getOptimisticTitle("conv_1")).toBeUndefined();
    expect(getOptimisticTitle("conv_2")).toBeUndefined();
  });
});
