import { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BottomSheet, BottomSheetContent } from "./bottom-sheet";

afterEach(cleanup);

let setSheetOpen: (open: boolean) => void = () => {};

function StatefulSheet({ onOpenChange }: { onOpenChange: (open: boolean) => void }) {
  const [open, setOpen] = useState(true);
  setSheetOpen = setOpen;
  return (
    <BottomSheet
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        setOpen(next);
      }}
    >
      <BottomSheetContent title="Pick one">
        <button type="button">Row</button>
      </BottomSheetContent>
    </BottomSheet>
  );
}

function renderSheet() {
  const onOpenChange = vi.fn();
  render(<StatefulSheet onOpenChange={onOpenChange} />);
  return onOpenChange;
}

const handle = () => screen.getByText("Pick one");
const sheet = () => screen.queryByRole("dialog", { name: "Pick one" });

function press(clientY: number) {
  fireEvent.pointerDown(handle(), { pointerId: 1, button: 0, clientY });
}
function move(clientY: number) {
  fireEvent.pointerMove(handle(), { pointerId: 1, clientY });
}
function release(clientY: number) {
  fireEvent.pointerUp(handle(), { pointerId: 1, clientY });
}

describe("BottomSheet", () => {
  it("names the dialog by its title and focuses the sheet, not its first row", () => {
    renderSheet();
    expect(sheet()).toHaveFocus();
  });

  it("follows the drag and closes when released far enough down", () => {
    const onOpenChange = renderSheet();
    press(100);
    move(220);
    expect(sheet()!.style.transform).toBe("translateY(120px)");
    release(220);
    expect(onOpenChange).toHaveBeenCalledWith(false);
    expect(sheet()).toBeNull();
  });

  it("springs back from a short drag", () => {
    const onOpenChange = renderSheet();
    press(100);
    move(130);
    expect(sheet()!.style.transform).toBe("translateY(30px)");
    release(130);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(sheet()!.style.transform).toBe("");
  });

  it("ignores pointer moves without a press", () => {
    renderSheet();
    move(220);
    expect(sheet()!.style.transform).toBe("");
  });

  it("drops an unfinished drag when it closes another way", () => {
    renderSheet();
    press(100);
    move(140);
    fireEvent.keyDown(sheet()!, { key: "Escape" });
    expect(sheet()).toBeNull();

    act(() => setSheetOpen(true));
    expect(sheet()!.style.transform).toBe("");
    move(240);
    expect(sheet()!.style.transform).toBe("");
  });

  it("closes on Escape", () => {
    const onOpenChange = renderSheet();
    fireEvent.keyDown(sheet()!, { key: "Escape" });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});
