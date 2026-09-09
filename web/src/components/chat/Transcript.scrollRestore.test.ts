import { describe, expect, it } from "vitest";
import { captureTranscriptViewSnapshot, resolveTranscriptViewOffset } from "./Transcript";

describe("transcript semantic scroll snapshots", () => {
  it("stores bottom as a mode instead of a pixel offset", () => {
    expect(
      captureTranscriptViewSnapshot({
        atBottom: true,
        scrollTop: 4200,
        anchor: { key: "user-8", start: 4000 },
      }),
    ).toEqual({ atBottom: true });
  });

  it("stores the visible bubble and its displacement from the viewport top", () => {
    expect(
      captureTranscriptViewSnapshot({
        atBottom: false,
        scrollTop: 4200,
        anchor: { key: "user-8", start: 4000 },
      }),
    ).toEqual({
      atBottom: false,
      anchorKey: "user-8",
      anchorOffset: -200,
      fallbackOffset: 4200,
    });
  });

  it("restores the same bubble after preceding row estimates change", () => {
    const snapshot = captureTranscriptViewSnapshot({
      atBottom: false,
      scrollTop: 4200,
      anchor: { key: "user-8", start: 4000 },
    });
    if (snapshot.atBottom) throw new Error("expected an anchor snapshot");

    expect(
      resolveTranscriptViewOffset(snapshot, (key) => (key === "user-8" ? 4600 : undefined)),
    ).toBe(4800);
  });

  it("uses the pixel fallback when the saved bubble is no longer loaded", () => {
    const snapshot = captureTranscriptViewSnapshot({
      atBottom: false,
      scrollTop: 4200,
      anchor: { key: "user-8", start: 4000 },
    });
    if (snapshot.atBottom) throw new Error("expected an anchor snapshot");

    expect(resolveTranscriptViewOffset(snapshot, () => undefined)).toBe(4200);
  });
});
