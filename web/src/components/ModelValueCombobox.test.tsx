import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { ModelValueCombobox } from "./ModelValueCombobox";

afterEach(cleanup);

const OPTIONS = ["opus", "sonnet", "haiku"];

/**
 * Stateful harness mirroring the call sites: ``onToggle`` adds/removes the
 * value from a selection kept as a comma-joined string, exactly like the
 * policy dialogs do.
 */
function Harness({ options = OPTIONS }: { options?: string[] }) {
  const [csv, setCsv] = useState("");
  const selected = csv ? csv.split(",").filter(Boolean) : [];
  return (
    <div>
      <ModelValueCombobox
        options={options}
        selected={selected}
        onToggle={(v) => {
          const next = selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v];
          setCsv(next.join(","));
        }}
      />
      <output data-testid="csv">{csv}</output>
    </div>
  );
}

function openList() {
  fireEvent.focus(screen.getByRole("textbox"));
}

describe("ModelValueCombobox", () => {
  it("opens the option list on focus", () => {
    render(<Harness />);
    expect(screen.queryByRole("button", { name: "opus" })).not.toBeInTheDocument();
    openList();
    expect(screen.getByRole("button", { name: "opus" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sonnet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "haiku" })).toBeInTheDocument();
  });

  it("selects an option on click and reflects it in the comma-joined value", () => {
    render(<Harness />);
    openList();
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    expect(screen.getByTestId("csv")).toHaveTextContent("opus");
  });

  it("keeps every option listed after selecting (does not remove picked ones)", () => {
    render(<Harness />);
    openList();
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    // The list stays open and still shows all three options.
    expect(screen.getByRole("button", { name: "opus" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "sonnet" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "haiku" })).toBeInTheDocument();
  });

  it("supports selecting multiple values in a row", () => {
    render(<Harness />);
    openList();
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    fireEvent.mouseDown(screen.getByRole("button", { name: "haiku" }));
    expect(screen.getByTestId("csv")).toHaveTextContent("opus,haiku");
  });

  it("toggles a selected value off when clicked again", () => {
    render(<Harness />);
    openList();
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    expect(screen.getByTestId("csv")).toHaveTextContent("opus");
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    expect(screen.getByTestId("csv")).toHaveTextContent("");
  });

  it("adds a free-form typed value on Enter", () => {
    render(<Harness />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "gpt-4o" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("csv")).toHaveTextContent("gpt-4o");
    // Query clears after committing.
    expect(input).toHaveValue("");
  });

  it("ignores Enter on blank / whitespace-only input", () => {
    render(<Harness />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("csv")).toHaveTextContent("");
  });

  it("filters options by the typed query (case-insensitive)", () => {
    render(<Harness />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "OP" } });
    expect(screen.getByRole("button", { name: "opus" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "sonnet" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "haiku" })).not.toBeInTheDocument();
  });

  it("hides the list when no option matches the query", () => {
    render(<Harness />);
    const input = screen.getByRole("textbox");
    fireEvent.change(input, { target: { value: "zzz" } });
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("closes the list on Escape", () => {
    render(<Harness />);
    const input = screen.getByRole("textbox");
    openList();
    expect(screen.getByRole("button", { name: "opus" })).toBeInTheDocument();
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("button", { name: "opus" })).not.toBeInTheDocument();
  });

  it("closes the list on outside pointer-down", () => {
    render(<Harness />);
    openList();
    expect(screen.getByRole("button", { name: "opus" })).toBeInTheDocument();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole("button", { name: "opus" })).not.toBeInTheDocument();
  });

  it("marks selected options with a visible checkmark and unselected ones without", () => {
    render(<Harness />);
    openList();
    fireEvent.mouseDown(screen.getByRole("button", { name: "opus" }));
    // The checkmark is the first svg child of each option button; selected
    // rows show it (no opacity-0), unselected rows keep it hidden.
    const selectedCheck = screen.getByRole("button", { name: "opus" }).querySelector("svg");
    const unselectedCheck = screen.getByRole("button", { name: "sonnet" }).querySelector("svg");
    expect(selectedCheck?.getAttribute("class")).not.toContain("opacity-0");
    expect(unselectedCheck?.getAttribute("class")).toContain("opacity-0");
  });

  it("uses a custom placeholder when provided", () => {
    render(
      <ModelValueCombobox
        options={OPTIONS}
        selected={[]}
        onToggle={vi.fn()}
        placeholder="Pick a model"
      />,
    );
    expect(screen.getByPlaceholderText("Pick a model")).toBeInTheDocument();
  });
});
