import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

if (typeof localStorage === "undefined" || typeof localStorage.clear !== "function") {
  const store = new Map<string, string>();
  const storage: Storage = {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key) => store.get(key) ?? null,
    key: (index) => Array.from(store.keys())[index] ?? null,
    removeItem: (key) => {
      store.delete(key);
    },
    setItem: (key, value) => {
      store.set(key, String(value));
    },
  };

  Object.defineProperty(globalThis, "localStorage", {
    configurable: true,
    value: storage,
  });
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: storage,
  });
}

// The @lobehub icon packages have broken nested-module resolution
// under vitest; stub presentational glyphs so component modules that
// import them can still load in tests.
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: () => null,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: () => null,
}));

// Radix UI primitives (DropdownMenu, etc.) call these pointer-capture and
// scroll APIs that jsdom doesn't implement. Stub them so component tests
// that open a Radix menu don't throw. No-ops are sufficient — the tests
// assert on the resulting DOM, not on capture/scroll side effects.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
}
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {};
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  }),
});
