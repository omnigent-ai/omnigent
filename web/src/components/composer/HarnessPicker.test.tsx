import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { HarnessPicker, HarnessPickerConfigPage, HarnessPickerEntry } from "./HarnessPicker";

afterEach(cleanup);

function PickerFixture({
  mobile = false,
  disabled = false,
}: {
  mobile?: boolean;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const config = <span>Model configuration</span>;
  return (
    <HarnessPicker
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setConfigOpen(false);
      }}
      trigger={{ label: "Harness", model: "Opus 4.8 (1M)", disabled }}
      tooltip="Current harness configuration"
      tooltipTestId="tooltip"
      testId="menu"
      configOpen={configOpen}
    >
      {mobile && configOpen ? (
        <HarnessPickerConfigPage onBack={() => setConfigOpen(false)} backTestId="back">
          {config}
        </HarnessPickerConfigPage>
      ) : (
        <HarnessPickerEntry
          icon={<span>Icon</span>}
          label="Claude Code"
          summary="Opus 4.8 (1M)"
          active
          isMobile={mobile}
          open={configOpen}
          onOpenChange={setConfigOpen}
          configContent={config}
          testId="entry"
          summaryTestId="model"
          editTestId="edit"
        />
      )}
    </HarnessPicker>
  );
}

describe("HarnessPicker", () => {
  it.each([false, true])("shares row geometry and config navigation on mobile=%s", (mobile) => {
    render(<PickerFixture mobile={mobile} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    expect(screen.getByTestId("menu")).toHaveClass("w-[17.5rem]", "p-2");
    expect(screen.getByTestId("entry")).toHaveClass("min-h-8", "gap-1", "bg-muted");
    expect(screen.getByTestId("model")).toHaveClass("text-right");
    expect(screen.getByTestId("edit")).toHaveTextContent("Edit");
    expect(screen.queryByTestId("tooltip")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("entry"));
    expect(screen.getByText("Model configuration")).toBeInTheDocument();
    if (mobile) {
      fireEvent.click(screen.getByTestId("back"));
      expect(screen.getByTestId("entry")).toBeInTheDocument();
      expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    }
  });

  it("does not open when the trigger is disabled", () => {
    render(<PickerFixture disabled />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
  });
});

describe("HarnessPickerEntry Edit flyout dismissal (#7069)", () => {
  function openConfig() {
    render(<PickerFixture />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    fireEvent.click(screen.getByTestId("entry"));
    expect(screen.getByText("Model configuration")).toBeInTheDocument();
  }

  it("closes the config flyout on a second click of the open row (pointer toggle)", () => {
    openConfig();
    // Second click on the already-open row toggles the flyout closed rather than
    // leaving it stuck open (pointer-move suppression blocks hover-out close).
    fireEvent.click(screen.getByTestId("entry"));
    expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    // The parent harness menu stays open so the user can pick another row.
    expect(screen.getByTestId("menu")).toBeInTheDocument();
  });

  it("closes only the config flyout on Escape and keeps the harness menu open (keyboard)", () => {
    openConfig();
    const flyout = screen.getByText("Model configuration").closest('[role="menu"]');
    expect(flyout).not.toBeNull();
    fireEvent.keyDown(flyout as HTMLElement, { key: "Escape" });
    expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    // Escape is scoped to the flyout: the parent menu must not also dismiss.
    expect(screen.getByTestId("menu")).toBeInTheDocument();
  });
});
