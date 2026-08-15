// Chord contract for ⌘⌥A: Cmd/Ctrl AND Alt, no Shift, physical KeyA (so ⌥a's
// "å" on macOS still matches), no auto-repeat, and inert when there's nothing
// archivable. Bare ⌘A must stay Select All.

import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useArchiveSessionHotkey } from "./useArchiveSessionHotkey";

function Harness({ onArchive }: { onArchive: (() => void) | null }) {
  useArchiveSessionHotkey(onArchive);
  return null;
}

function press(init: Partial<KeyboardEventInit> & { code?: string }) {
  window.dispatchEvent(
    new KeyboardEvent("keydown", { code: "KeyA", key: "a", bubbles: true, ...init }),
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("useArchiveSessionHotkey", () => {
  it("fires on Cmd+Alt+A", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true, altKey: true });
    expect(onArchive).toHaveBeenCalledTimes(1);
  });

  it("fires on Ctrl+Alt+A (Windows/Linux)", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ ctrlKey: true, altKey: true });
    expect(onArchive).toHaveBeenCalledTimes(1);
  });

  it("matches the physical key, so macOS ⌥a → 'å' still archives", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true, altKey: true, key: "å" });
    expect(onArchive).toHaveBeenCalledTimes(1);
  });

  it("ignores bare Cmd+A — that's Select All", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true });
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("ignores the chord with Shift, leaving ⌘⌥⇧ free", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true, altKey: true, shiftKey: true });
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("ignores other letters on the same chord", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true, altKey: true, code: "KeyB", key: "b" });
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("ignores auto-repeat so holding the chord doesn't re-PATCH", () => {
    const onArchive = vi.fn();
    render(<Harness onArchive={onArchive} />);
    press({ metaKey: true, altKey: true, repeat: true });
    expect(onArchive).not.toHaveBeenCalled();
  });

  it("is inert when there's nothing archivable", () => {
    // No handler is registered to call, and the chord must fall through
    // untouched rather than being swallowed.
    const { container } = render(<Harness onArchive={null} />);
    expect(container).toBeEmptyDOMElement();
    const event = new KeyboardEvent("keydown", {
      code: "KeyA",
      key: "a",
      metaKey: true,
      altKey: true,
      cancelable: true,
    });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
  });

  it("unbinds on unmount", () => {
    const onArchive = vi.fn();
    const { unmount } = render(<Harness onArchive={onArchive} />);
    unmount();
    press({ metaKey: true, altKey: true });
    expect(onArchive).not.toHaveBeenCalled();
  });
});
