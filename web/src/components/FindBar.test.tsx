import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TooltipProvider } from "@/components/ui/tooltip";
import { DEFAULT_FIND_OPTIONS, FindBar, type FindBarProps } from "./FindBar";

afterEach(cleanup);

function renderBar(overrides: Partial<FindBarProps> = {}) {
  const props: FindBarProps = {
    label: "Find in file",
    query: "alpha",
    onQueryChange: vi.fn(),
    status: "2 of 5",
    hasMatches: true,
    onNext: vi.fn(),
    onPrevious: vi.fn(),
    onClose: vi.fn(),
    focusRequest: 1,
    testIdPrefix: "file-find",
    ...overrides,
  };
  render(
    <TooltipProvider>
      <FindBar {...props} />
    </TooltipProvider>,
  );
  return props;
}

describe("FindBar", () => {
  it("focuses and selects the query when opened", () => {
    renderBar();
    const input = screen.getByRole("textbox", { name: "Find in file" });
    expect(input).toHaveFocus();
    expect((input as HTMLInputElement).selectionEnd).toBe(5);
    expect(screen.getByTestId("file-find-status")).toHaveTextContent("2 of 5");
  });

  it("steps with Enter and Shift+Enter, and closes with Escape from any control", () => {
    const props = renderBar();
    const input = screen.getByRole("textbox", { name: "Find in file" });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(props.onNext).toHaveBeenCalledTimes(1);
    expect(props.onPrevious).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("button", { name: "Next match" }), { key: "Escape" });
    expect(props.onClose).toHaveBeenCalledTimes(1);
  });

  it("disables stepping without matches", () => {
    renderBar({ hasMatches: false, status: "No results" });
    expect(screen.getByRole("button", { name: "Previous match" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next match" })).toBeDisabled();
  });

  it("shows option toggles only when the caller supports them", () => {
    renderBar();
    expect(screen.queryByRole("button", { name: "Match case" })).toBeNull();
    cleanup();
    const onOptionsChange = vi.fn();
    renderBar({ options: DEFAULT_FIND_OPTIONS, onOptionsChange });
    fireEvent.click(screen.getByRole("button", { name: "Match whole word" }));
    expect(onOptionsChange).toHaveBeenCalledWith({ ...DEFAULT_FIND_OPTIONS, wholeWord: true });
    expect(screen.getByRole("textbox", { name: "Find in file" })).toHaveFocus();
  });

  it("docked in a terminal's row, draws its text, field and controls in the terminal's colors", () => {
    renderBar({ docked: { fold: 0 } });
    const bar = screen.getByRole("search");
    // The row repaints --foreground, --border and --muted; the field and buttons read these.
    expect(bar.className).toContain("text-foreground");
    expect(bar.className).not.toContain("text-popover-foreground");
    expect(bar.style.getPropertyValue("--input")).toBe("var(--border)");
    expect(bar.style.getPropertyValue("--ring")).toBe("var(--muted-foreground)");
    expect(bar.style.getPropertyValue("--accent")).toBe("var(--muted)");
    expect(bar.style.getPropertyValue("--accent-foreground")).toBe("var(--foreground)");
  });

  describe("replace row", () => {
    const MOD = /Mac/.test(navigator.platform) ? { metaKey: true } : { ctrlKey: true };
    function renderReplace(overrides: Partial<NonNullable<FindBarProps["replace"]>> = {}) {
      const replace = {
        value: "omega",
        onChange: vi.fn(),
        open: true,
        onOpenChange: vi.fn(),
        onReplace: vi.fn(),
        onReplaceAll: vi.fn(),
        ...overrides,
      };
      const props = renderBar({ replace, focusTarget: "replace" });
      return { props, replace };
    }

    it("is absent unless the caller can replace", () => {
      renderBar();
      expect(screen.queryByRole("button", { name: "Replace" })).toBeNull();
      expect(screen.queryByRole("textbox", { name: "Replace with" })).toBeNull();
    });

    it("toggles open from the button before the query", () => {
      const { replace } = renderReplace({ open: false });
      const toggle = screen.getByRole("button", { name: "Replace" });
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      expect(screen.queryByRole("textbox", { name: "Replace with" })).toBeNull();
      fireEvent.click(toggle);
      expect(replace.onOpenChange).toHaveBeenCalledWith(true);
    });

    it("replaces with Enter, replaces all with the command key, and closes with Escape", () => {
      const { props, replace } = renderReplace();
      const field = screen.getByRole("textbox", { name: "Replace with" });
      expect(field).toHaveFocus();
      expect(field).toHaveValue("omega");
      fireEvent.change(field, { target: { value: "beta" } });
      expect(replace.onChange).toHaveBeenCalledWith("beta");
      fireEvent.keyDown(field, { key: "Enter" });
      expect(replace.onReplace).toHaveBeenCalledTimes(1);
      fireEvent.keyDown(field, { key: "Enter", ...MOD });
      expect(replace.onReplaceAll).toHaveBeenCalledTimes(1);
      expect(props.onNext).not.toHaveBeenCalled();
      fireEvent.click(screen.getByRole("button", { name: "Replace all" }));
      expect(replace.onReplaceAll).toHaveBeenCalledTimes(2);
      expect(field).toHaveFocus();
      fireEvent.keyDown(field, { key: "Escape" });
      expect(props.onClose).toHaveBeenCalled();
    });

    it("disables replacing without matches", () => {
      cleanup();
      const replace = {
        value: "",
        onChange: vi.fn(),
        open: true,
        onOpenChange: vi.fn(),
        onReplace: vi.fn(),
        onReplaceAll: vi.fn(),
      };
      renderBar({ replace, hasMatches: false, status: "No results" });
      expect(screen.getByRole("button", { name: "Replace match" })).toBeDisabled();
      expect(screen.getByRole("button", { name: "Replace all" })).toBeDisabled();
    });
  });
});
