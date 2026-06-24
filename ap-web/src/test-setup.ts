import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

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
