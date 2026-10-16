import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { SettingsLabel } from "./SettingsLabel";

afterEach(cleanup);

describe("SettingsLabel", () => {
  it("moves mobile help into an inline tooltip while retaining the desktop description", async () => {
    render(
      <SettingsLabel
        label="Interface font size"
        description="Set text across the interface."
        descriptionId="font-size-description"
      />,
    );

    const description = document.getElementById("font-size-description");
    expect(description).toHaveClass("max-md:hidden");
    expect(screen.getByText("Interface font size")).toHaveClass("font-normal", "md:font-medium");

    const trigger = screen.getByRole("button", { name: "About Interface font size" });
    expect(trigger).toHaveClass("md:hidden");

    fireEvent.click(trigger);
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Set text across the interface.");
  });
});
