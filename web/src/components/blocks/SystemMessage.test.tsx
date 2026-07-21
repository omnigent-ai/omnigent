import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SystemMessageView } from "./SystemMessage";

afterEach(cleanup);

describe("SystemMessageView", () => {
  it("uses compact Otto card geometry for expanded system details", () => {
    render(
      <SystemMessageView
        message={{
          kind: "generic",
          label: "Environment updated",
          body: "PATH was refreshed",
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /system: environment updated/i }));
    expect(screen.getByText("PATH was refreshed")).toHaveClass(
      "rounded-md",
      "border-[var(--border-otto-hairline)]",
    );
  });

  it("hides sub-agent wake notices instead of rendering a centered System row", () => {
    const { container } = render(
      <SystemMessageView
        message={{
          kind: "subagent_wake",
          label: "Sub-agent result ready",
          body: "",
        }}
      />,
    );

    expect(screen.queryByTestId("system-message")).toBeNull();
    expect(container.textContent).toBe("");
  });
});
