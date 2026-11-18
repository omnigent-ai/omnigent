import { describe, expect, it } from "vitest";
import { nextTranscriptBottomPin, type TranscriptBottomPin } from "./transcriptBottomPin";

const UNPINNED: TranscriptBottomPin = { pinned: false, anchorScrollTop: 0 };

/** Feed scroll positions through the pin, returning each event's pinned state. */
function pinnedAfterEach(
  positions: { scrollTop: number; scrollHeight?: number; clientHeight?: number }[],
): boolean[] {
  let pin = UNPINNED;
  return positions.map(({ scrollTop, scrollHeight = 1700, clientHeight = 700 }) => {
    pin = nextTranscriptBottomPin(pin, { scrollTop, scrollHeight, clientHeight });
    return pin.pinned;
  });
}

describe("nextTranscriptBottomPin", () => {
  it("pins within 1px of the bottom and not further up", () => {
    expect(pinnedAfterEach([{ scrollTop: 999 }])).toEqual([true]);
    expect(pinnedAfterEach([{ scrollTop: 998 }])).toEqual([false]);
  });

  it("keeps the pin when a scroll reads a viewport shortened by pending composer growth", () => {
    // The pin parks 1px short of the bottom, then its own scroll event reads a
    // viewport 24px short before the overlay re-measures.
    expect(
      pinnedAfterEach([
        { scrollTop: 999 },
        { scrollTop: 999, clientHeight: 676 },
        { scrollTop: 999, clientHeight: 700 },
      ]),
    ).toEqual([true, true, true]);
  });

  it("keeps the pin after rows remeasure shorter and clamp scrollTop down at the bottom", () => {
    expect(
      pinnedAfterEach([
        { scrollTop: 999 },
        { scrollTop: 899, scrollHeight: 1600 },
        { scrollTop: 899, scrollHeight: 1600, clientHeight: 676 },
      ]),
    ).toEqual([true, true, true]);
  });

  it("releases the pin once small upward steps add up to more than the 1px allowance", () => {
    expect(
      pinnedAfterEach([
        { scrollTop: 1000 },
        { scrollTop: 999.5 },
        { scrollTop: 999 },
        { scrollTop: 998.5 },
        { scrollTop: 998 },
        { scrollTop: 997.5 },
      ]),
    ).toEqual([true, true, true, true, true, false]);
  });

  it("releases the pin on one upward scroll of more than 1px while the viewport is short", () => {
    expect(pinnedAfterEach([{ scrollTop: 999 }, { scrollTop: 990, clientHeight: 676 }])).toEqual([
      true,
      false,
    ]);
  });

  it("stays released until the reader returns to the bottom", () => {
    expect(pinnedAfterEach([{ scrollTop: 900 }, { scrollTop: 950 }, { scrollTop: 999 }])).toEqual([
      false,
      false,
      true,
    ]);
  });
});
