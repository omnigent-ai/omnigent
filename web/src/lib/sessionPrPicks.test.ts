import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { clearSessionPrPicks, setSessionPrPick, useSessionPrPick } from "./sessionPrPicks";

const firstPr = "https://github.com/example/one/pull/1";
const secondPr = "https://github.com/example/one/pull/2";

describe("session PR picks", () => {
  beforeEach(clearSessionPrPicks);
  afterEach(cleanup);

  it("keeps a separate pick for each session and forgets one on undefined", () => {
    const first = renderHook(() => useSessionPrPick("session_a"));
    const second = renderHook(() => useSessionPrPick("session_b"));

    act(() => {
      setSessionPrPick("session_a", firstPr);
      setSessionPrPick("session_b", secondPr);
    });
    expect(first.result.current).toBe(firstPr);
    expect(second.result.current).toBe(secondPr);

    act(() => setSessionPrPick("session_a", undefined));
    expect(first.result.current).toBeUndefined();
    expect(second.result.current).toBe(secondPr);
  });

  it("clears every session's pick", () => {
    const { result } = renderHook(() => useSessionPrPick("session_a"));
    act(() => setSessionPrPick("session_a", firstPr));
    act(clearSessionPrPicks);
    expect(result.current).toBeUndefined();
  });
});
