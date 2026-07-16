import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ThemeColorPicker } from "./ThemeColorPicker";

describe("ThemeColorPicker", () => {
  it("opens a custom spectrum popover and commits a valid hex value", () => {
    const onChange = vi.fn();
    render(
      <ThemeColorPicker label="Accent" value="#0969da" testId="theme-accent" onChange={onChange} />,
    );

    expect(screen.queryByTestId("theme-accent-spectrum")).toBeNull();
    fireEvent.click(screen.getByTestId("theme-accent-trigger"));
    expect(screen.getByTestId("theme-accent-spectrum")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("theme-accent-input"), {
      target: { value: "#2563eb" },
    });
    expect(onChange).toHaveBeenLastCalledWith("#2563eb");
  });

  it("updates the color from the hue rail", () => {
    const onChange = vi.fn();
    render(
      <ThemeColorPicker label="Accent" value="#ff0000" testId="theme-accent" onChange={onChange} />,
    );

    fireEvent.click(screen.getByTestId("theme-accent-trigger"));
    fireEvent.change(screen.getByTestId("theme-accent-hue"), { target: { value: "120" } });
    expect(onChange).toHaveBeenLastCalledWith("#00ff00");
  });

  it("randomizes to a vivid color", () => {
    const onChange = vi.fn();
    const random = vi.spyOn(Math, "random").mockReturnValue(0.5);
    render(
      <ThemeColorPicker label="Accent" value="#ff0000" testId="theme-accent" onChange={onChange} />,
    );

    fireEvent.click(screen.getByTestId("theme-accent-trigger"));
    fireEvent.click(screen.getByRole("button", { name: "Randomize accent" }));

    expect(onChange).toHaveBeenLastCalledWith("#3ad2d2");
    random.mockRestore();
  });
});
