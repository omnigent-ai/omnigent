import { cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { QuickPick } from "@/components/ui/quick-pick";
import { TooltipProvider } from "@/components/ui/tooltip";
import { EmbeddedProvider } from "@/lib/embedded";
import type * as NativeBridgeModule from "@/lib/nativeBridge";
import { setEmbedRoot } from "@/lib/host";

import { isNewSessionHotkey, useNewSessionHotkey } from "./useNewSessionHotkey";

const navigate = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
const shell = vi.hoisted(() => ({ native: false }));
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<typeof NativeBridgeModule>()),
  isNativeShell: () => shell.native,
}));

afterEach(() => {
  cleanup();
  setEmbedRoot(null);
  navigate.mockReset();
  shell.native = false;
  document.body.innerHTML = "";
});

function event(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent("keydown", { bubbles: true, cancelable: true, ...init });
}

function press(init: KeyboardEventInit, target: HTMLElement = document.body): KeyboardEvent {
  const e = event(init);
  target.dispatchEvent(e);
  return e;
}

describe("isNewSessionHotkey", () => {
  it("uses Cmd on macOS and Ctrl on other platforms", () => {
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }), true),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }), true),
    ).toBe(false);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }), false),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it("matches the displayed O across keyboard layouts", () => {
    expect(
      isNewSessionHotkey(event({ key: "O", code: "KeyS", ctrlKey: true, shiftKey: true }), false),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "o", code: "KeyS", metaKey: true, shiftKey: true }), true),
    ).toBe(true);
    expect(
      isNewSessionHotkey(event({ key: "R", code: "KeyO", ctrlKey: true, shiftKey: true }), false),
    ).toBe(false);
  });

  it.each([true, false])("rejects modified chords and unrelated keys with isMac=%s", (isMac) => {
    const command = { metaKey: isMac, ctrlKey: !isMac };
    for (const init of [
      { key: "O", code: "KeyO" },
      { key: "O", code: "KeyO", altKey: true, shiftKey: true },
      { key: "M", code: "KeyM", shiftKey: true },
      { key: "N", code: "KeyN", altKey: true, shiftKey: true },
      { key: "O", code: "KeyO", ctrlKey: true, metaKey: true, shiftKey: true },
    ]) {
      expect(isNewSessionHotkey(event({ ...command, ...init }), isMac)).toBe(false);
    }
    const altGraph = event({ ...command, key: "O", code: "KeyO", shiftKey: true });
    vi.spyOn(altGraph, "getModifierState").mockImplementation((key) => key === "AltGraph");
    expect(isNewSessionHotkey(altGraph, isMac)).toBe(false);
  });

  it("uses Cmd+N in native shells, without the older Alt chord, and not in browsers", () => {
    const native = event({ key: "n", code: "KeyN", metaKey: true });
    expect(isNewSessionHotkey(native, true, true)).toBe(true);
    expect(isNewSessionHotkey(native, true, false)).toBe(false);
    const old = event({ key: "˜", code: "KeyN", metaKey: true, altKey: true });
    expect(isNewSessionHotkey(old, true, true)).toBe(false);
    expect(isNewSessionHotkey(old, true, false)).toBe(false);
    expect(
      isNewSessionHotkey(
        event({ key: "O", code: "KeyO", metaKey: true, shiftKey: true }),
        true,
        true,
      ),
    ).toBe(false);
    expect(isNewSessionHotkey(event({ key: "n", code: "KeyN", ctrlKey: true }), false, true)).toBe(
      true,
    );
    expect(
      isNewSessionHotkey(
        event({ key: "n", code: "KeyN", ctrlKey: true, altKey: true }),
        false,
        true,
      ),
    ).toBe(false);
  });

  it.each([
    ["⌘", { metaKey: true }, true],
    ["Ctrl", { ctrlKey: true }, false],
  ])("on another layout, %s+N follows the key that types N", (_name, modifier, isMac) => {
    // Dvorak types B at the physical N key, and N at the physical L key.
    expect(isNewSessionHotkey(event({ key: "n", code: "KeyL", ...modifier }), isMac, true)).toBe(
      true,
    );
    expect(isNewSessionHotkey(event({ key: "b", code: "KeyN", ...modifier }), isMac, true)).toBe(
      false,
    );
  });
});

describe("useNewSessionHotkey", () => {
  it("navigates to the shared new-session route and claims Ctrl+Shift+O", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });

    expect(navigate).toHaveBeenCalledWith("/");
    expect(e.defaultPrevented).toBe(true);
  });

  it("claims the displayed O from a different physical key", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    const e = press({ key: "O", code: "KeyS", ctrlKey: true, shiftKey: true });

    expect(e.defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
  });

  it("works from editable fields so the global action is focus-independent", () => {
    renderHook(() => useNewSessionHotkey(true, false));
    const input = document.createElement("input");
    document.body.appendChild(input);
    input.focus();

    press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }, input);

    expect(navigate).toHaveBeenCalledWith("/");
  });

  it("handles embedded input without claiming shortcuts in the host page", () => {
    const root = document.createElement("div");
    const input = document.createElement("input");
    root.appendChild(input);
    document.body.appendChild(root);
    setEmbedRoot(root);
    renderHook(() => useNewSessionHotkey(true, false), { wrapper: EmbeddedProvider });

    const outside = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });
    expect(outside.defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();

    const inside = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true }, input);
    expect(inside.defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenCalledExactlyOnceWith("/");
  });

  it("ignores auto-repeat", () => {
    renderHook(() => useNewSessionHotkey(true, false));

    press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true, repeat: true });

    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves the shortcut to the host page when disabled", () => {
    renderHook(() => useNewSessionHotkey(false, false));

    const e = press({ key: "O", code: "KeyO", ctrlKey: true, shiftKey: true });

    expect(navigate).not.toHaveBeenCalled();
    expect(e.defaultPrevented).toBe(false);
  });
});

describe("useNewSessionHotkey with an open pick list (native Windows/Linux)", () => {
  it("leaves a chord a focused field already handled, even outside a pick list", () => {
    shell.native = true;
    renderHook(() => useNewSessionHotkey(true, false));
    const field = document.createElement("input");
    field.addEventListener("keydown", (keydown) => keydown.preventDefault());
    document.body.append(field);
    press({ key: "n", code: "KeyN", ctrlKey: true }, field);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("leaves Ctrl-N to the list's field, and still starts a session elsewhere", () => {
    shell.native = true;
    renderHook(() => useNewSessionHotkey(true, false));
    const field = document.createElement("input");
    field.setAttribute("data-pick-list-input", "");
    document.body.append(field);
    const ctrlN = { key: "n", code: "KeyN", ctrlKey: true };
    expect(press(ctrlN, field).defaultPrevented).toBe(false);
    expect(navigate).not.toHaveBeenCalled();
    expect(press(ctrlN).defaultPrevented).toBe(true);
    expect(navigate).toHaveBeenCalledWith("/");
  });
});

/** The app's new-session hotkey beside a real QuickPick, as on a native Windows or Linux shell. */
function PickerBesideHotkey(
  props: Pick<ComponentProps<typeof QuickPick>, "label" | "presentation" | "stepsWithCtrlNP"> & {
    rows: string[];
  },
) {
  useNewSessionHotkey(true, false);
  const { rows, ...picker } = props;
  return (
    <TooltipProvider>
      <QuickPick
        {...picker}
        segments={[
          {
            sourceId: "rows",
            ranking: "client",
            items: rows.map((id) => ({ id, label: id, value: id })),
          },
        ]}
        query=""
        onQueryChange={vi.fn()}
        emptyMessage="No results"
        onAccept={vi.fn()}
      />
    </TooltipProvider>
  );
}

/** Focuses the picker's field and presses Ctrl-N in it. */
function pressCtrlNIn(label: string): HTMLElement {
  const field = screen.getByRole("combobox", { name: label });
  field.focus();
  fireEvent.keyDown(field, { key: "n", code: "KeyN", ctrlKey: true });
  return field;
}

describe("useNewSessionHotkey with a real QuickPick (native Windows/Linux)", () => {
  it("lets Ctrl-N step a ⌘K picker without starting a session", () => {
    shell.native = true;
    render(<PickerBesideHotkey label="Review picker" stepsWithCtrlNP rows={["one", "two"]} />);
    const field = pressCtrlNIn("Review picker");
    expect(document.getElementById(field.getAttribute("aria-activedescendant")!)).toHaveTextContent(
      "two",
    );
    expect(navigate).not.toHaveBeenCalled();
  });

  it("still starts a session from a sidebar search that shows no list yet", () => {
    shell.native = true;
    render(<PickerBesideHotkey label="Search settings" presentation="sidebar" rows={["Theme"]} />);
    pressCtrlNIn("Search settings");
    expect(navigate).toHaveBeenCalledOnce();
  });
});
