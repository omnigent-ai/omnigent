import { useState } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HarnessPicker, HarnessPickerConfigRow } from "./HarnessPicker";
import { DropdownMenuItem } from "@/components/ui/dropdown-menu";

function ConfigPicker({ disabledEffort = false }: { disabledEffort?: boolean }) {
  const [open, setOpen] = useState(false);
  const [section, setSection] = useState<string | null>(null);
  return (
    <HarnessPicker
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setSection(null);
      }}
      trigger={{ label: "Configure session", model: "Default" }}
      testId="picker"
    >
      {[
        { id: "model", label: "Model", value: "Default" },
        { id: "effort", label: "Thinking level", value: "Low" },
      ].map(({ id, label, value }) => (
        <HarnessPickerConfigRow
          key={id}
          label={label}
          value={value}
          open={section === id}
          onOpenChange={(next) =>
            setSection((current) => (next ? id : current === id ? null : current))
          }
          isMobile={false}
          disabled={id === "effort" && disabledEffort}
          testId={`${id}-row`}
          configTestId={`${id}-menu`}
        >
          <DropdownMenuItem data-testid={`${id}-option`}>{value}</DropdownMenuItem>
        </HarnessPickerConfigRow>
      ))}
    </HarnessPicker>
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function openPicker() {
  fireEvent.pointerDown(screen.getByRole("button", { name: "Configure session" }), {
    button: 0,
    pointerType: "mouse",
  });
}

function leaveTowardFlyout(from: string, to: string) {
  const row = screen.getByTestId(`${from}-row`);
  const menu = screen.getByTestId(`${from}-menu`);
  // Nonzero geometry makes the diagonal path cross the sibling inside Radix's grace area.
  vi.spyOn(menu, "getBoundingClientRect").mockReturnValue(new DOMRect(200, 0, 150, 200));
  menu.dataset.side = "right";
  fireEvent.pointerMove(row, { pointerType: "mouse", clientX: 50, clientY: 80 });
  fireEvent.pointerLeave(row, {
    pointerType: "mouse",
    clientX: 100,
    clientY: 80,
    relatedTarget: screen.getByTestId(`${to}-row`),
  });
}

describe("HarnessPickerConfigRow hover", () => {
  it.each([
    ["model", "effort"],
    ["effort", "model"],
  ])(
    "switches %s to %s even when the pointer stops inside the old flyout's grace area",
    (from, to) => {
      render(<ConfigPicker />);
      openPicker();
      fireEvent.click(screen.getByTestId(`${from}-row`));
      expect(screen.getByTestId(`${from}-menu`)).toBeVisible();
      vi.useFakeTimers();
      leaveTowardFlyout(from, to);
      const next = screen.getByTestId(`${to}-row`);
      fireEvent.pointerEnter(next, {
        pointerType: "mouse",
        clientX: 150,
        clientY: 90,
        relatedTarget: screen.getByTestId(`${from}-row`),
      });
      fireEvent.pointerMove(next, { pointerType: "mouse", clientX: 150, clientY: 90 });
      // No more pointer events: expiry of the grace timeout must not leave the old menu stuck.
      act(() => vi.advanceTimersByTime(500));
      expect(screen.queryByTestId(`${from}-menu`)).toBeNull();
      expect(screen.getByTestId(`${to}-menu`)).toBeVisible();
      expect(next).toHaveFocus();
      expect(screen.getAllByRole("menu")).toHaveLength(2);
    },
  );

  it("does not switch to a disabled sibling on mouse entry", () => {
    render(<ConfigPicker disabledEffort />);
    openPicker();
    fireEvent.click(screen.getByTestId("model-row"));
    fireEvent.pointerEnter(screen.getByTestId("effort-row"), { pointerType: "mouse" });
    expect(screen.getByTestId("model-menu")).toBeVisible();
    expect(screen.queryByTestId("effort-menu")).toBeNull();
  });

  it.each(["touch", "pen"])("does not open on %s pointer entry", (pointerType) => {
    render(<ConfigPicker />);
    openPicker();
    fireEvent.pointerEnter(screen.getByTestId("model-row"), { pointerType });
    expect(screen.queryByTestId("model-menu")).toBeNull();
  });

  it("keeps the submenu open when moving through the gap into its options", () => {
    render(<ConfigPicker />);
    openPicker();
    fireEvent.click(screen.getByTestId("model-row"));
    const row = screen.getByTestId("model-row");
    const option = screen.getByTestId("model-option");
    fireEvent.pointerLeave(row, { pointerType: "mouse", relatedTarget: option });
    fireEvent.pointerEnter(option, { pointerType: "mouse", relatedTarget: row });
    fireEvent.pointerMove(option, { pointerType: "mouse" });
    expect(screen.getByTestId("model-menu")).toBeVisible();
    expect(screen.queryByTestId("effort-menu")).toBeNull();
  });

  it("preserves keyboard submenu navigation and Escape dismissal", () => {
    render(<ConfigPicker />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Configure session" }), {
      key: "ArrowDown",
    });
    fireEvent.keyDown(screen.getByTestId("model-row"), { key: "ArrowRight" });
    expect(screen.getByTestId("model-menu")).toBeVisible();
    fireEvent.keyDown(screen.getByTestId("model-option"), { key: "ArrowLeft" });
    expect(screen.queryByTestId("model-menu")).toBeNull();
    expect(screen.getByTestId("model-row")).toHaveFocus();
    fireEvent.keyDown(screen.getByTestId("effort-row"), { key: "ArrowRight" });
    expect(screen.getByTestId("effort-menu")).toBeVisible();
    fireEvent.keyDown(screen.getByTestId("effort-option"), { key: "Escape" });
    expect(screen.queryByTestId("picker")).toBeNull();
  });
});
