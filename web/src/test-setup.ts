import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Default the event-stream transport to SSE in tests. Production defaults to
// the WebSocket (it dodges the ~6-per-origin HTTP/1.1 pool), but the extensive
// `startStreamPump` reconnect/bind suite drives the SSE fetch path by mocking
// `openSessionStream`, and that fallback still ships. The WebSocket transport
// has its own dedicated coverage (`sessionEventSocket.test.ts` +
// `tests/server/routes/test_session_stream_ws.py`). A WS-branch test can
// override this per-case with `vi.stubEnv("VITE_EVENT_STREAM_TRANSPORT", ...)`.
vi.stubEnv("VITE_EVENT_STREAM_TRANSPORT", "sse");

// The @lobehub icon packages have broken nested-module resolution
// under vitest; stub presentational glyphs so component modules that
// import them can still load in tests. (The Antigravity glyph additionally
// drags in @lobehub/fluent-emoji → @emoji-mart/data, whose JSON modules need
// an import attribute Node refuses under vitest — so it must be stubbed too.)
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: () => null,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: () => null,
}));
vi.mock("@/components/icons/OpenCodeIcon", () => ({
  OpenCodeIcon: () => null,
}));
vi.mock("@/components/icons/CursorIcon", () => ({
  CursorIcon: () => null,
}));
vi.mock("@/components/icons/GooseIcon", () => ({
  GooseIcon: () => null,
}));
vi.mock("@/components/icons/KiroIcon", () => ({
  KiroIcon: () => null,
}));
vi.mock("@/components/icons/AntigravityIcon", () => ({
  AntigravityIcon: () => null,
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

// jsdom doesn't implement IntersectionObserver (used by the sidebar's
// infinite-scroll sentinel). A no-op stub is enough — tests that need to drive
// auto-loading can override the global with their own controllable mock.
if (!("IntersectionObserver" in globalThis)) {
  class MockIntersectionObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
    root = null;
    rootMargin = "";
    thresholds = [];
  }
  Object.defineProperty(globalThis, "IntersectionObserver", {
    writable: true,
    configurable: true,
    value: MockIntersectionObserver,
  });
}

// cmdk (the command-palette primitive) constructs a ResizeObserver on mount,
// which jsdom doesn't implement. A no-op stub lets command-palette/selector
// component tests render without throwing.
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  };
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
