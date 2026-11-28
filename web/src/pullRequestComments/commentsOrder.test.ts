import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import {
  COMMENTS_ORDER_STORAGE_KEY,
  commentsOrderPreference,
  useCommentsOrder,
} from "./commentsOrder";

afterEach(() => localStorage.clear());

it("is newest first until another order is chosen, and ignores an unknown stored one", () => {
  expect(commentsOrderPreference.read()).toBe("newest");
  localStorage.setItem(COMMENTS_ORDER_STORAGE_KEY, "sideways");
  expect(commentsOrderPreference.read()).toBe("newest");
  commentsOrderPreference.write("oldest");
  expect(localStorage.getItem(COMMENTS_ORDER_STORAGE_KEY)).toBe("oldest");
  expect(commentsOrderPreference.read()).toBe("oldest");
});

it("follows a choice made in another window", () => {
  const { result } = renderHook(() => useCommentsOrder());
  expect(result.current).toBe("newest");
  act(() => {
    localStorage.setItem(COMMENTS_ORDER_STORAGE_KEY, "file");
    window.dispatchEvent(new StorageEvent("storage", { key: COMMENTS_ORDER_STORAGE_KEY }));
  });
  expect(result.current).toBe("file");
});
