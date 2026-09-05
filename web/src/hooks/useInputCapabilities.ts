import { useSyncExternalStore } from "react";

export interface InputCapabilities {
  anyCoarse: boolean;
  hasTouch: boolean;
}

const COARSE_POINTER_QUERY = "(any-pointer: coarse)";

const SERVER_SNAPSHOT: InputCapabilities = {
  anyCoarse: false,
  hasTouch: false,
};

let mediaList: MediaQueryList | null = null;
const subscribers = new Set<() => void>();

function list(): MediaQueryList {
  return (mediaList ??= window.matchMedia(COARSE_POINTER_QUERY));
}

function read(): InputCapabilities {
  if (typeof window === "undefined" || !window.matchMedia) return SERVER_SNAPSHOT;
  return {
    anyCoarse: list().matches,
    hasTouch: navigator.maxTouchPoints > 0,
  };
}

let cached: InputCapabilities = SERVER_SNAPSHOT;

function getSnapshot(): InputCapabilities {
  const next = read();
  if (next.anyCoarse !== cached.anyCoarse || next.hasTouch !== cached.hasTouch) {
    cached = next;
  }
  return cached;
}

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  if (subscribers.size === 0) list().addEventListener("change", emit);
  subscribers.add(callback);
  return () => {
    subscribers.delete(callback);
    if (subscribers.size !== 0 || !mediaList) return;
    mediaList.removeEventListener("change", emit);
    mediaList = null;
  };
}

function emit() {
  for (const subscriber of subscribers) subscriber();
}

export function useInputCapabilities(): InputCapabilities {
  return useSyncExternalStore(subscribe, getSnapshot, () => SERVER_SNAPSHOT);
}
