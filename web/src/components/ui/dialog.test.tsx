import { cleanup, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "./dialog";

// The iOS shell keeps the WKWebView layout viewport full-height when the soft
// keyboard opens, so a modal capped at `85vh` and centered on `50%` would sit
// partly behind the keyboard. DialogContent pins its height cap and centering
// origin to the keyboard-aware `--omnigent-viewport-height` — but only inside
// the iOS shell. These tests pin that gating.

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

function renderDialog() {
  return render(
    <Dialog open onOpenChange={() => {}}>
      <DialogContent>
        <DialogTitle>Test</DialogTitle>
      </DialogContent>
    </Dialog>,
  );
}

describe("DialogContent keyboard-aware sizing", () => {
  it("caps height and centering to the visible viewport inside the iOS shell", () => {
    setIOS(true);
    renderDialog();
    const content = screen.getByRole("dialog");
    // Inline style (not a class) so it wins over any caller's max-h-[85vh].
    expect(content.style.maxHeight).toContain("--omnigent-viewport-height");
    expect(content.style.top).toContain("--omnigent-viewport-height");
  });

  it("applies no viewport inline style off the iOS shell", () => {
    setIOS(false);
    renderDialog();
    const content = screen.getByRole("dialog");
    expect(content.style.maxHeight).toBe("");
    expect(content.style.top).toBe("");
  });
});

describe("DialogContent column sizing", () => {
  it("uses a single zero-minimum column so unbreakable content cannot widen the panel", () => {
    renderDialog();
    expect(screen.getByRole("dialog")).toHaveClass("grid-cols-[minmax(0,1fr)]");
  });
});

function ConfirmDialog({
  focusConfirm,
  irreversible,
}: {
  focusConfirm?: boolean;
  irreversible?: boolean;
}) {
  const confirm = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open onOpenChange={() => {}}>
      <DialogContent
        showCloseButton={false}
        initialFocus={focusConfirm ? confirm : undefined}
        irreversible={irreversible}
      >
        <DialogTitle>Close the pull request?</DialogTitle>
        <button type="button">Cancel</button>
        <button type="button" ref={confirm}>
          Close
        </button>
      </DialogContent>
    </Dialog>
  );
}

describe("DialogContent initial focus", () => {
  it("focuses the first control without initialFocus, irreversible or not", () => {
    renderConfirm({});
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    cleanup();
    renderConfirm({ irreversible: true });
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("focuses initialFocus's element on open", () => {
    renderConfirm({ focusConfirm: true });
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });
});

function renderConfirm(props: { focusConfirm?: boolean; irreversible?: boolean }) {
  return render(<ConfirmDialog {...props} />);
}
