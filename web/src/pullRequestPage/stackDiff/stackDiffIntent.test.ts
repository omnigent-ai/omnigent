import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  STACK_DIFF_INTENT_LIMIT,
  stackDiffIntentMemory,
  useStackDiffIntent,
} from "./stackDiffIntent";

const URL = "https://git.example.com/acme/app/pull/102";
const ALICE = '["mac-read-as","alice"]';
const BOB = '["mac-read-as","bob"]';

afterEach(() => {
  window.localStorage.clear();
  stackDiffIntentMemory.resetUnsaved();
});

describe("useStackDiffIntent", () => {
  it("starts on Automatic and keeps a choice on this device", () => {
    const { result, unmount } = renderHook(() => useStackDiffIntent(ALICE, URL));
    expect(result.current[0]).toBe("automatic");
    act(() => result.current[1]("this-pr"));
    expect(result.current[0]).toBe("this-pr");
    unmount();
    expect(renderHook(() => useStackDiffIntent(ALICE, URL.toUpperCase())).result.current[0]).toBe(
      "this-pr",
    );
  });

  it("keeps each reader's choice apart", () => {
    const alice = renderHook(() => useStackDiffIntent(ALICE, URL));
    act(() => alice.result.current[1]("all"));
    expect(renderHook(() => useStackDiffIntent(BOB, URL)).result.current[0]).toBe("automatic");
  });

  it("stores Automatic as nothing, so it keeps following the policy", () => {
    const { result } = renderHook(() => useStackDiffIntent(ALICE, URL));
    act(() => result.current[1]("all"));
    act(() => result.current[1]("automatic"));
    expect(stackDiffIntentMemory.read(ALICE).size).toBe(0);
  });

  it("keeps nothing without a reader", () => {
    const { result } = renderHook(() => useStackDiffIntent(null, URL));
    act(() => result.current[1]("this-pr"));
    expect(result.current[0]).toBe("automatic");
    expect(window.localStorage.length).toBe(0);
  });

  it("forgets the oldest choices past its limit", () => {
    for (let number = 0; number <= STACK_DIFF_INTENT_LIMIT; number += 1) {
      stackDiffIntentMemory.write(ALICE, `${URL}/${number}`, "all");
    }
    const kept = stackDiffIntentMemory.read(ALICE);
    expect(kept.size).toBe(STACK_DIFF_INTENT_LIMIT);
    expect(kept.has(`${URL}/0`)).toBe(false);
  });
});
