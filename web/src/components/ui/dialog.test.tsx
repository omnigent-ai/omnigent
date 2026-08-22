import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog";

// A dialog is `position: fixed`, so it anchors to the LAYOUT viewport — which
// stays tall while the mobile URL bar, the soft keyboard, or the home indicator
// cover its bottom. Capping at `85vh` and centering on `50%` therefore puts the
// panel's bottom edge at 92.5% of the *large* viewport: off-screen, with the
// footer buttons unreachable. DialogContent now owns that safety for every
// caller and every platform by sizing/centering against `--omnigent-dialog-*`
// (index.css → the live `visualViewport`, less the safe-area insets) and by
// scrolling its own middle. jsdom does no layout, so these tests pin the
// contract that produces the behavior.

function setIOS(on: boolean): void {
  if (on) {
    (window as unknown as Record<string, unknown>).omnigentNative = { kind: "ios" };
  } else {
    delete (window as unknown as Record<string, unknown>).omnigentNative;
  }
}

afterEach(() => {
  cleanup();
  setIOS(false);
});

function renderDialog(props?: { className?: string }) {
  return render(
    <Dialog open onOpenChange={() => {}}>
      <DialogContent className={props?.className}>
        <DialogHeader>
          <DialogTitle>Test</DialogTitle>
        </DialogHeader>
        <p>body</p>
        <DialogFooter>
          <button type="button">Save</button>
        </DialogFooter>
      </DialogContent>
    </Dialog>,
  );
}

function content(): HTMLElement {
  return screen.getByRole("dialog");
}

describe("DialogContent viewport safety", () => {
  it("caps and centers against the visible viewport on every platform", () => {
    renderDialog();
    // Centering origin AND height cap both track the visible band; a static
    // cap alone is not enough, since a panel centered on the layout viewport
    // hangs below it (cap must satisfy `cap <= 2*svh - lvh` otherwise).
    expect(content().className).toContain("top-[var(--omnigent-dialog-center)]");
    expect(content().className).toContain("max-h-[var(--omnigent-dialog-max-height)]");
    // Utilities, not inline style, so callers can still override (below).
    expect(content().style.top).toBe("");
    expect(content().style.maxHeight).toBe("");
  });

  it("applies the same viewport-locked sizing inside the iOS shell", () => {
    // Previously the only platform that got any of this, via inline style. The
    // iOS shell still drives it — `useVisibleViewportHeight` publishes the
    // keyboard-aware `--omnigent-viewport-height` that `--omnigent-dialog-*`
    // resolve through — it is just no longer the only platform that benefits.
    setIOS(true);
    renderDialog();
    expect(content().className).toContain("top-[var(--omnigent-dialog-center)]");
    expect(content().className).toContain("max-h-[var(--omnigent-dialog-max-height)]");
  });

  it("is a flex column that scrolls its middle, with header and footer pinned", () => {
    renderDialog();
    expect(content().className).toContain("flex-col");

    const body = content().querySelector('[data-slot="dialog-body"]');
    expect(body).not.toBeNull();
    // `min-h-0` is what lets a flex child shrink below its content and scroll.
    expect(body!.className).toContain("min-h-0");
    expect(body!.className).toContain("flex-1");
    expect(body!.className).toContain("overflow-y-auto");

    expect(body!.textContent).toContain("body");
    // Header and footer sit OUTSIDE the scroller, so the buttons never scroll
    // away — the whole point of the fix.
    expect(body!.contains(screen.getByText("Test"))).toBe(false);
    expect(body!.contains(screen.getByRole("button", { name: "Save" }))).toBe(false);
    expect(content().querySelector('[data-slot="dialog-footer"]')!.parentElement).toBe(content());
  });

  it("sees through a bare fragment to the footer inside it", () => {
    // Several callers group a conditional branch's fields AND its footer in one
    // `<>…</>`. A fragment is not a layout box, so the footer must still land
    // outside the scroller rather than scrolling away with the fields.
    render(
      <Dialog open onOpenChange={() => {}}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Test</DialogTitle>
          </DialogHeader>
          <>
            <p>fields</p>
            <DialogFooter>
              <button type="button">Save</button>
            </DialogFooter>
          </>
        </DialogContent>
      </Dialog>,
    );
    const body = content().querySelector('[data-slot="dialog-body"]')!;
    expect(body.textContent).toContain("fields");
    expect(body.contains(screen.getByRole("button", { name: "Save" }))).toBe(false);
    expect(content().querySelector('[data-slot="dialog-footer"]')!.parentElement).toBe(content());
  });

  it("renders no scroller for a confirm dialog that is only header + footer", () => {
    render(
      <Dialog open onOpenChange={() => {}}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete?</DialogTitle>
          </DialogHeader>
          <DialogFooter>
            <button type="button">Delete</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    );
    // Nothing to scroll, so no empty wrapper — the footer is still a direct
    // child of the panel and the cap still applies.
    expect(content().querySelector('[data-slot="dialog-body"]')).toBeNull();
    expect(content().querySelector('[data-slot="dialog-footer"]')!.parentElement).toBe(content());
    expect(content().className).toContain("max-h-[var(--omnigent-dialog-max-height)]");
  });

  it("lets a caller override the cap and the centering origin", () => {
    // The command palette deliberately sits high (`top-1/4`); twMerge must let
    // that win over the shared default.
    renderDialog({ className: "top-1/4 max-h-[50vh]" });
    expect(content().className).toContain("top-1/4");
    expect(content().className).toContain("max-h-[50vh]");
    expect(content().className).not.toContain("--omnigent-dialog-center");
    expect(content().className).not.toContain("--omnigent-dialog-max-height");
  });

  it("does not nest a second scroller around a caller-supplied DialogBody", () => {
    render(
      <Dialog open onOpenChange={() => {}}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Test</DialogTitle>
          </DialogHeader>
          <DialogBody data-testid="mine">fields</DialogBody>
          <DialogFooter>
            <button type="button">Save</button>
          </DialogFooter>
        </DialogContent>
      </Dialog>,
    );
    expect(content().querySelectorAll('[data-slot="dialog-body"]')).toHaveLength(1);
    expect(screen.getByTestId("mine").parentElement).toBe(content());
  });

  it("keeps a sibling's state when a conditional child toggles", () => {
    // The middle is re-parented into one wrapper; keying it by original
    // position keeps a conditional row from remounting its neighbours.
    function Harness() {
      return (
        <Dialog open onOpenChange={() => {}}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Test</DialogTitle>
            </DialogHeader>
            <input aria-label="name" defaultValue="" />
            <button type="button" onClick={() => {}}>
              noop
            </button>
          </DialogContent>
        </Dialog>
      );
    }
    const { rerender } = render(<Harness />);
    const input = screen.getByLabelText("name") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "typed" } });
    rerender(<Harness />);
    expect((screen.getByLabelText("name") as HTMLInputElement).value).toBe("typed");
  });
});

describe("DialogFooter", () => {
  it("reserves the bottom safe-area / native-bar inset", () => {
    renderDialog();
    const footer = content().querySelector('[data-slot="dialog-footer"]')!;
    // Buttons must clear the home indicator / gesture bar; 0px off mobile, so
    // desktop spacing is unchanged.
    expect(footer.className).toContain("pb-[max(1.5rem,var(--omnigent-inset-bottom))]");
    // Listed after `p-6` so twMerge keeps both.
    expect(footer.className.indexOf("p-6")).toBeLessThan(
      footer.className.indexOf("pb-[max(1.5rem"),
    );
  });
});
