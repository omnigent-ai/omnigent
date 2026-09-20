import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import {
  HarnessPicker,
  HarnessPickerConfigPage,
  HarnessPickerEntry,
  HarnessPickerSubContent,
} from "./HarnessPicker";
import {
  DropdownMenuItem,
  DropdownMenuSub,
  DropdownMenuSubTrigger,
} from "@/components/ui/dropdown-menu";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function PickerFixture({
  mobile = false,
  disabled = false,
  tooltipVariant = "default",
  nested = false,
  active = true,
  modal,
}: {
  modal?: boolean;
  mobile?: boolean;
  disabled?: boolean;
  tooltipVariant?: "default" | "session-info";
  nested?: boolean;
  active?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  // A real focusable option (role=menuitem) so the Escape test can start focus
  // on an actual flyout option, not the menu container.
  const config = (
    <DropdownMenuItem data-testid="config-option">Model configuration</DropdownMenuItem>
  );
  const entry = (
    <HarnessPickerEntry
      icon={<span>Icon</span>}
      label="Claude Code"
      summary="Opus 4.8 (1M)"
      active={active}
      isMobile={mobile}
      open={configOpen}
      onOpenChange={setConfigOpen}
      configContent={config}
      configTestId="config-menu"
      testId="entry"
      summaryTestId="model"
      editTestId="edit"
    />
  );
  return (
    <HarnessPicker
      modal={modal}
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setConfigOpen(false);
      }}
      trigger={{ label: "Harness", model: "Opus 4.8 (1M)", disabled }}
      tooltip="Current harness configuration"
      tooltipTestId="tooltip"
      tooltipVariant={tooltipVariant}
      testId="menu"
      configOpen={configOpen}
    >
      {mobile && configOpen ? (
        <HarnessPickerConfigPage onBack={() => setConfigOpen(false)} backTestId="back">
          {config}
        </HarnessPickerConfigPage>
      ) : nested ? (
        <DropdownMenuSub>
          <DropdownMenuSubTrigger data-testid="other">Other...</DropdownMenuSubTrigger>
          <HarnessPickerSubContent className="composer-agent-menu" data-testid="other-menu">
            {entry}
          </HarnessPickerSubContent>
        </DropdownMenuSub>
      ) : (
        entry
      )}
    </HarnessPicker>
  );
}

function GraceAreaPicker() {
  const [open, setOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  return (
    <HarnessPicker
      open={open}
      onOpenChange={setOpen}
      trigger={{ label: "Harness", model: "Default" }}
      testId="menu"
    >
      <DropdownMenuSub>
        <DropdownMenuSubTrigger data-testid="other">Other...</DropdownMenuSubTrigger>
        <HarnessPickerSubContent data-testid="other-menu">
          <DropdownMenuItem data-testid="last-harness">Last harness</DropdownMenuItem>
        </HarnessPickerSubContent>
      </DropdownMenuSub>
      <HarnessPickerEntry
        icon={<span>Icon</span>}
        label="Polly"
        summary="Default"
        active={false}
        open={configOpen}
        onOpenChange={setConfigOpen}
        configContent={<DropdownMenuItem>Model configuration</DropdownMenuItem>}
        testId="sibling"
        configTestId="sibling-config"
      />
    </HarnessPicker>
  );
}

describe("HarnessPicker", () => {
  it.each(["right", "left"])(
    "keeps a %s submenu open while crossing a harness row toward its lower items",
    (side) => {
      render(<GraceAreaPicker />);
      fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
      const other = screen.getByTestId("other");
      fireEvent.click(other);
      const submenu = screen.getByTestId("other-menu");
      // Mirror the diagonal path for flyouts flipped by viewport collisions.
      vi.spyOn(submenu, "getBoundingClientRect").mockReturnValue(
        new DOMRect(side === "right" ? 200 : 0, 0, 100, 300),
      );
      submenu.dataset.side = side;
      const x = (rightX: number) => (side === "right" ? rightX : 300 - rightX);
      fireEvent.pointerMove(other, { pointerType: "mouse", clientX: x(60), clientY: 80 });
      fireEvent.pointerMove(other, { pointerType: "mouse", clientX: x(80), clientY: 80 });
      const sibling = screen.getByTestId("sibling");
      fireEvent.pointerLeave(other, {
        pointerType: "mouse",
        clientX: x(100),
        clientY: 80,
        relatedTarget: sibling,
      });
      fireEvent.pointerMove(sibling, { pointerType: "mouse", clientX: x(150), clientY: 120 });
      expect(submenu).toBeInTheDocument();
      expect(other).toHaveFocus();
      expect(screen.queryByTestId("sibling-config")).not.toBeInTheDocument();

      const last = screen.getByTestId("last-harness");
      fireEvent.pointerLeave(sibling, {
        pointerType: "mouse",
        clientX: x(190),
        clientY: 180,
        relatedTarget: last,
      });
      fireEvent.pointerMove(last, { pointerType: "mouse", clientX: x(210), clientY: 200 });
      expect(submenu).toBeInTheDocument();
      expect(last).toHaveFocus();
      fireEvent.click(last);
      expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
    },
  );

  it("focuses a hovered harness without opening configuration, then allows keyboard editing", async () => {
    render(<GraceAreaPicker />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    fireEvent.click(screen.getByTestId("other"));
    const sibling = screen.getByTestId("sibling");
    vi.useFakeTimers();
    fireEvent.pointerMove(sibling, { pointerType: "mouse", clientX: 50, clientY: 150 });
    act(() => vi.advanceTimersByTime(500));
    expect(sibling).toHaveFocus();
    expect(screen.queryByTestId("other-menu")).not.toBeInTheDocument();
    expect(screen.queryByTestId("sibling-config")).not.toBeInTheDocument();
    vi.useRealTimers();
    fireEvent.keyDown(sibling, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByText("Model configuration")).toHaveFocus());
  });

  it.each([false, true])("shares row geometry and config navigation on mobile=%s", (mobile) => {
    render(<PickerFixture mobile={mobile} />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    expect(screen.getByTestId("menu")).toHaveClass("w-[17.5rem]", "min-w-[17.5rem]", "p-2");
    expect(screen.getByTestId("entry").closest("[data-harness-menu-row]")).toHaveClass("min-h-8");
    expect(screen.getByTestId("entry")).toHaveAttribute("data-active", "true");
    expect(screen.getByTestId("model")).toHaveClass("text-right");
    expect(screen.getByTestId("edit")).toHaveTextContent("Edit");
    expect(screen.getByTestId("edit")).toHaveClass("hover:underline");
    expect(screen.queryByTestId("tooltip")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("edit"));
    expect(screen.getByText("Model configuration")).toBeInTheDocument();
    if (mobile) {
      fireEvent.click(screen.getByTestId("back"));
      expect(screen.getByTestId("entry")).toBeInTheDocument();
      expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    }
  });

  it("keeps a non-modal picker open while focusing its active row inside a dialog", async () => {
    const user = userEvent.setup();
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>Configure automation</DialogTitle>
          <DialogDescription>Choose the harness for this automation.</DialogDescription>
          <PickerFixture />
        </DialogContent>
      </Dialog>,
    );

    await user.click(screen.getByRole("button", { name: "Harness" }));

    await waitFor(() => expect(screen.getByTestId("menu")).toBeInTheDocument());
    expect(screen.getByTestId("entry")).toHaveFocus();
  });

  it.each([0, 1, 2])(
    "lets the first outside click reach its target with submenu depth %s",
    async (depth) => {
      // Flyout hover geometry needs a browser; this case verifies click/focus delivery.
      const user = userEvent.setup({ skipHover: true });
      const onOutsideClick = vi.fn();
      render(
        <>
          <textarea aria-label="Message" />
          <button type="button" onClick={onOutsideClick}>
            Other control
          </button>
          <PickerFixture nested={depth === 2} />
        </>,
      );
      const trigger = screen.getByRole("button", { name: "Harness" });
      const openPicker = async () => {
        await user.click(trigger);
        if (depth === 2) await user.click(screen.getByTestId("other"));
        if (depth > 0) await user.click(screen.getByTestId("edit"));
        expect(screen.getAllByRole("menu")).toHaveLength(depth + 1);
      };
      await openPicker();
      const textarea = screen.getByRole("textbox", { name: "Message" });
      await user.click(textarea);
      await waitFor(() => expect(screen.queryByTestId("menu")).not.toBeInTheDocument());
      expect(textarea).toHaveFocus();
      await user.keyboard("continue typing");
      expect(textarea).toHaveValue("continue typing");

      await openPicker();
      const outside = screen.getByRole("button", { name: "Other control" });
      await user.click(outside);
      await waitFor(() => expect(screen.queryByTestId("menu")).not.toBeInTheDocument());
      expect(outside).toHaveFocus();
      expect(onOutsideClick).toHaveBeenCalledTimes(1);
      await user.tab();
      expect(trigger).toHaveFocus();
      expect(trigger).not.toHaveAttribute("data-pointer-interaction");

      await openPicker();
      await user.keyboard("{Escape}");
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
    },
  );

  it("preserves pointer-vs-keyboard trigger ring behavior for explicit modal embeddings", async () => {
    render(<PickerFixture modal />);
    const trigger = screen.getByRole("button", { name: "Harness" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByTestId("entry")).toHaveFocus());
    fireEvent.pointerDown(document.body, { button: 0 });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("data-pointer-interaction", "true");
    expect(trigger).toHaveClass("data-[pointer-interaction=true]:focus-visible:ring-0");

    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByTestId("entry")).toHaveFocus());
    fireEvent.keyDown(screen.getByTestId("entry"), { key: "Escape" });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).not.toHaveAttribute("data-pointer-interaction");
  });

  it("does not carry modal pointer ring suppression into a later keyboard visit", async () => {
    render(<PickerFixture modal />);
    const trigger = screen.getByRole("button", { name: "Harness" });
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    await waitFor(() => expect(screen.getByTestId("entry")).toHaveFocus());
    fireEvent.pointerDown(document.body, { button: 0 });
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(trigger).toHaveAttribute("data-pointer-interaction", "true");
    fireEvent.blur(trigger);
    expect(trigger).not.toHaveAttribute("data-pointer-interaction");
  });

  it("does not suppress later keyboard entry after hovering an unfocused closed trigger", () => {
    render(<PickerFixture />);
    const trigger = screen.getByRole("button", { name: "Harness" });
    fireEvent.pointerMove(trigger, { pointerType: "mouse" });
    fireEvent.focus(trigger);
    expect(trigger).not.toHaveAttribute("data-pointer-interaction");
    fireEvent.keyDown(trigger, { key: "ArrowDown" });
    expect(screen.getByTestId("menu")).toHaveAttribute("data-input-method", "keyboard");
  });

  it("keeps pointer mode when focus transfers from the trigger into the menu", async () => {
    render(<PickerFixture />);
    const trigger = screen.getByRole("button", { name: "Harness" });
    trigger.focus();
    fireEvent.pointerDown(trigger, { button: 0 });
    await waitFor(() => expect(screen.getByTestId("entry")).toHaveFocus());
    expect(screen.getByTestId("menu")).toHaveAttribute("data-input-method", "pointer");
  });

  it("switches focus styling with the last input without blurring the focused row", async () => {
    render(<PickerFixture active={false} />);
    fireEvent.keyDown(screen.getByRole("button", { name: "Harness" }), { key: "ArrowDown" });
    const entry = screen.getByTestId("entry");
    await waitFor(() => expect(entry).toHaveFocus());
    expect(screen.getByTestId("model")).toHaveClass("group-focus-within/agent:opacity-100");
    fireEvent.pointerMove(entry, { pointerType: "mouse" });
    expect(entry).toHaveFocus();
    expect(screen.getByTestId("menu")).toHaveAttribute("data-input-method", "pointer");
    expect(screen.getByTestId("model")).not.toHaveClass("group-focus-within/agent:opacity-100");
    fireEvent.keyDown(entry, { key: "ArrowRight" });
    await waitFor(() => expect(screen.getByTestId("config-option")).toHaveFocus());
    expect(screen.getByTestId("menu")).toHaveAttribute("data-input-method", "keyboard");
    expect(screen.getByTestId("config-menu")).toHaveAttribute("data-input-method", "keyboard");
    expect(entry).toHaveAttribute("data-state", "open");
  });

  it("shares input mode across nested portals without closing expanded parents on pointer exit", () => {
    render(<PickerFixture nested />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    fireEvent.click(screen.getByTestId("other"));
    fireEvent.click(screen.getByTestId("edit"));
    expect(screen.getAllByRole("menu")).toHaveLength(3);
    fireEvent.pointerMove(screen.getByTestId("config-option"), { pointerType: "mouse" });
    for (const id of ["menu", "other-menu", "config-menu"]) {
      expect(screen.getByTestId(id)).toHaveAttribute("data-input-method", "pointer");
    }
    expect(screen.getByTestId("other")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("entry")).toHaveAttribute("data-state", "open");
    fireEvent.pointerLeave(screen.getByTestId("menu"), { relatedTarget: document.body });
    expect(screen.getByTestId("other")).toHaveAttribute("data-state", "open");
    expect(screen.getByTestId("entry")).toHaveAttribute("data-state", "open");
    fireEvent.click(screen.getByTestId("edit"));
    expect(screen.queryByTestId("config-menu")).not.toBeInTheDocument();
    expect(screen.getByTestId("entry")).toHaveAttribute("data-state", "closed");
  });

  it("does not open when the trigger is disabled", () => {
    render(<PickerFixture disabled />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
  });

  it("can use the light session-info tooltip surface without changing tooltip defaults", async () => {
    render(<PickerFixture tooltipVariant="session-info" />);
    fireEvent.focus(screen.getByRole("button", { name: "Harness" }));
    expect(await screen.findByTestId("tooltip")).toHaveClass(
      "w-64",
      "rounded-lg",
      "bg-popover",
      "p-2.5",
      "text-popover-foreground",
      "shadow-menu",
      "ring-1",
    );
  });
});

describe("HarnessPickerEntry Edit flyout dismissal (#7069)", () => {
  function openConfig() {
    render(<PickerFixture />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    fireEvent.click(screen.getByTestId("edit"));
    expect(screen.getByText("Model configuration")).toBeInTheDocument();
  }

  it("keeps the config flyout closed when the row is clicked", () => {
    render(<PickerFixture />);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Harness" }), { button: 0 });
    fireEvent.click(screen.getByTestId("entry"));
    expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
  });

  it("closes the config flyout on a second click of Edit (pointer toggle)", () => {
    openConfig();
    fireEvent.click(screen.getByTestId("edit"));
    expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    // The parent harness menu stays open so the user can pick another row.
    expect(screen.getByTestId("menu")).toBeInTheDocument();
  });

  it("dismisses the whole menu on Escape from a focused flyout option (keyboard)", () => {
    openConfig();
    // Start focus on an actual flyout OPTION, not the menu container.
    const option = screen.getByTestId("config-option");
    option.focus();
    const flyout = screen.getByText("Model configuration").closest<HTMLElement>('[role="menu"]');
    expect(flyout).not.toBeNull();
    expect(flyout!.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    // Accepted #7069 behavior: Escape dismisses the flyout AND the parent menu
    // (Radix's nested-menu Escape) — it is not scoped to the sub. The
    // parent-stays-open "close just this flyout" path is the second-click
    // pointer toggle above. Root-close + focus-to-root-trigger is confirmed in
    // the real-browser CDP check.
    expect(screen.queryByText("Model configuration")).not.toBeInTheDocument();
    expect(screen.queryByTestId("menu")).not.toBeInTheDocument();
  });
});
