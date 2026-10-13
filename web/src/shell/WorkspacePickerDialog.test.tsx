import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WorkspacePickerDialog } from "./WorkspacePickerDialog";

vi.mock("./WorkspacePicker", () => ({
  isNavigablePath: (path: string) => path.startsWith("/"),
  WorkspacePicker: ({
    initialPath,
    onSelect,
    onClose,
  }: {
    initialPath?: string;
    onSelect: (path: string) => void;
    onClose: () => void;
  }) => (
    <div data-testid="mock-workspace-picker" data-initial-path={initialPath}>
      <button type="button" onClick={() => onSelect("/chosen")}>
        Confirm mock
      </button>
      <button type="button" onClick={onClose}>
        Cancel mock
      </button>
    </div>
  ),
}));

function Harness({
  onConfirm = vi.fn(),
  onCancel = vi.fn(),
}: {
  onConfirm?: (path: string) => void;
  onCancel?: () => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Reopen
      </button>
      <WorkspacePickerDialog
        open={open}
        onOpenChange={setOpen}
        hostId="host_1"
        initialPath="/initial"
        onConfirm={onConfirm}
        onCancel={onCancel}
      />
    </>
  );
}

describe("WorkspacePickerDialog", () => {
  it("uses the canonical dialog shell and commits only on Confirm", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<Harness onConfirm={onConfirm} onCancel={onCancel} />);

    expect(screen.getByTestId("workspace-picker-dialog")).toBeInTheDocument();
    expect(screen.getByTestId("mock-workspace-picker")).toHaveAttribute(
      "data-initial-path",
      "/initial",
    );

    fireEvent.click(screen.getByRole("button", { name: "Confirm mock" }));

    expect(onConfirm).toHaveBeenCalledWith("/chosen");
    expect(onCancel).not.toHaveBeenCalled();
    expect(screen.queryByTestId("workspace-picker-dialog")).toBeNull();
  });

  it("cancels without committing and starts fresh when reopened", () => {
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    render(<Harness onConfirm={onConfirm} onCancel={onCancel} />);

    fireEvent.click(screen.getByRole("button", { name: "Cancel mock" }));

    expect(onCancel).toHaveBeenCalledOnce();
    expect(onConfirm).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Reopen" }));
    expect(screen.getByTestId("mock-workspace-picker")).toHaveAttribute(
      "data-initial-path",
      "/initial",
    );
  });

  it("treats Escape as cancellation", () => {
    const onCancel = vi.fn();
    render(<Harness onCancel={onCancel} />);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(onCancel).toHaveBeenCalledOnce();
    expect(screen.queryByTestId("workspace-picker-dialog")).toBeNull();
  });
});
