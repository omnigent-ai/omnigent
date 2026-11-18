import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import {
  PEER_SESSION_MESSAGES_STORAGE_KEY,
  readPeerSessionMessageDisplay,
  usePeerSessionMessageDisplay,
  writePeerSessionMessageDisplay,
} from "./peerSessionMessagePreferences";

// Covers the key and choices; storedChoicePreference.test.ts covers shared storage.
afterEach(() => {
  localStorage.clear();
});

it("collapses by default and only accepts a known choice", () => {
  expect(readPeerSessionMessageDisplay()).toBe("collapse");
  localStorage.setItem(PEER_SESSION_MESSAGES_STORAGE_KEY, "nope");
  expect(readPeerSessionMessageDisplay()).toBe("collapse");

  writePeerSessionMessageDisplay("hide");
  expect(localStorage.getItem(PEER_SESSION_MESSAGES_STORAGE_KEY)).toBe("hide");
  expect(readPeerSessionMessageDisplay()).toBe("hide");

  writePeerSessionMessageDisplay("collapse");
  expect(localStorage.getItem(PEER_SESSION_MESSAGES_STORAGE_KEY)).toBeNull();
});

it("updates a mounted reader on a local write", () => {
  const { result } = renderHook(() => usePeerSessionMessageDisplay());
  expect(result.current).toBe("collapse");
  act(() => writePeerSessionMessageDisplay("show"));
  expect(result.current).toBe("show");
});
