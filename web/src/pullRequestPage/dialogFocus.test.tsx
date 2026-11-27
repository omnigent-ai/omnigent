import { cleanup, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, expect, it } from "vitest";

import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";

import { focusOnOpen } from "./dialogFocus";

afterEach(cleanup);

function ConfirmDialog({ focusConfirm, disabled }: { focusConfirm: boolean; disabled?: boolean }) {
  const confirm = useRef<HTMLButtonElement>(null);
  return (
    <Dialog open onOpenChange={() => {}}>
      <DialogContent
        showCloseButton={false}
        onOpenAutoFocus={focusConfirm ? focusOnOpen(confirm) : undefined}
      >
        <DialogTitle>Turn on auto-merge?</DialogTitle>
        <button type="button">Cancel</button>
        <button type="button" ref={confirm} disabled={disabled}>
          Turn on
        </button>
      </DialogContent>
    </Dialog>
  );
}

it("focuses the first control by default", () => {
  render(<ConfirmDialog focusConfirm={false} />);
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
});

it("focuses the target on open", () => {
  render(<ConfirmDialog focusConfirm />);
  expect(screen.getByRole("button", { name: "Turn on" })).toHaveFocus();
});

it("leaves the default when the target can't take focus", () => {
  render(<ConfirmDialog focusConfirm disabled />);
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
});
