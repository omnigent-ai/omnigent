import { describe, expect, it } from "vitest";
import { parseVideoChapters, readChapterCues } from "./videoChapters";

function cues(rows: { startTime: number; endTime: number; text: string }[]): TextTrackCueList {
  return rows as unknown as TextTrackCueList;
}

describe("WebVTT chapters", () => {
  it("retains native cue ranges and plain titles, orders cues, and removes duplicate rows", () => {
    const open = { startTime: 0, endTime: 4, text: "Open the app" };
    expect(
      readChapterCues(
        cues([
          { startTime: 5, endTime: 8, text: "Verify <script>plain text</script>" },
          open,
          open,
        ]),
      ),
    ).toEqual([
      { time: 0, end: 4, title: "Open the app" },
      { time: 5, end: 8, title: "Verify <script>plain text</script>" },
    ]);
  });

  it("bounds cue lists and excludes empty or oversized labels", () => {
    expect(
      readChapterCues(
        cues(Array.from({ length: 201 }, () => ({ startTime: 0, endTime: 1, text: "Many" }))),
      ),
    ).toEqual([]);
    expect(
      readChapterCues(
        cues([
          { startTime: 0, endTime: 1, text: " " },
          { startTime: 1, endTime: 2, text: "x".repeat(501) },
        ]),
      ),
    ).toEqual([]);
    expect(readChapterCues(null)).toEqual([]);
  });

  it("avoids native parsing for cancelled and oversized reads", async () => {
    const controller = new AbortController();
    controller.abort();
    expect(await parseVideoChapters("WEBVTT\n", controller.signal)).toEqual([]);
    expect(await parseVideoChapters("x".repeat(65_537), new AbortController().signal)).toEqual([]);
  });
});
