import { act, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type ThingyComposerQuote,
  queueThingyComposerQuote,
  resetThingyComposerQuotes,
  useThingyComposerQuotes,
} from "./thingyComposerQuotes";

function Surface({
  sessionId,
  apply,
}: {
  sessionId: string | null;
  apply: (quote: ThingyComposerQuote) => boolean;
}) {
  useThingyComposerQuotes(sessionId, apply);
  return null;
}

afterEach(() => {
  resetThingyComposerQuotes();
  vi.useRealTimers();
});

describe("thingy composer quotes", () => {
  it("adds quotes for this session in order and leaves other sessions' quotes", () => {
    const apply = vi.fn((_quote: ThingyComposerQuote) => true);
    render(<Surface sessionId="conv_1" apply={apply} />);

    act(() => {
      queueThingyComposerQuote("conv_1", "reply", "first");
      queueThingyComposerQuote("conv_2", "reply", "elsewhere");
      queueThingyComposerQuote("conv_1", "side_chat", "second");
    });

    expect(apply.mock.calls.map(([quote]) => [quote.kind, quote.text])).toEqual([
      ["reply", "first"],
      ["side_chat", "second"],
    ]);
  });

  it("keeps a quote until the composer can take it, then adds it once", () => {
    let ready = false;
    const apply = vi.fn((_quote: ThingyComposerQuote) => ready);
    const { rerender } = render(<Surface sessionId="conv_1" apply={apply} />);
    act(() => queueThingyComposerQuote("conv_1", "reply", "waiting"));
    expect(apply).toHaveBeenCalledTimes(1);

    ready = true;
    rerender(<Surface sessionId="conv_1" apply={apply} />);
    rerender(<Surface sessionId="conv_1" apply={apply} />);
    expect(apply).toHaveBeenCalledTimes(2);
  });

  it("waits for its session, and drops a quote that session never shows", () => {
    vi.useFakeTimers();
    act(() => queueThingyComposerQuote("conv_1", "reply", "stale"));
    const apply = vi.fn(() => true);
    const { rerender } = render(<Surface sessionId={null} apply={apply} />);
    expect(apply).not.toHaveBeenCalled();

    vi.advanceTimersByTime(61_000);
    rerender(<Surface sessionId="conv_1" apply={apply} />);
    expect(apply).not.toHaveBeenCalled();
  });
});
