import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposerSettingsButton } from "./ComposerSettingsButton";

afterEach(cleanup);

describe("ComposerSettingsButton", () => {
  it("opens settings as a distinct labeled composer action", () => {
    const onClick = vi.fn();
    render(<ComposerSettingsButton data-testid="settings" onClick={onClick} />);

    const button = screen.getByRole("button", { name: "Advanced settings" });
    expect(button).toHaveAttribute("title", "Advanced settings");
    expect(button).toHaveTextContent("Advanced settings");
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("marks only its text label for responsive collapse", () => {
    render(<ComposerSettingsButton />);
    const button = screen.getByRole("button", { name: "Advanced settings" });
    expect(button.querySelector("svg")).toHaveClass("size-4", "shrink-0");
    expect(screen.getByText("Advanced settings")).toHaveClass(
      "group-data-[labels=collapsed]/composer-actions:hidden",
    );
  });
});
