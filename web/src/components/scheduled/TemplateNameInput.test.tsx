import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { splitNameTemplate, TemplateNameInput } from "./TemplateNameInput";

describe("splitNameTemplate", () => {
  it.each([
    ["nightly", [{ kind: "text", text: "nightly" }]],
    [
      "Test Automation {{MMM DD}}",
      [
        { kind: "text", text: "Test Automation " },
        { kind: "placeholder", text: "{{MMM DD}}" },
      ],
    ],
    [
      "from {{MMM}} to {{DD}}",
      [
        { kind: "text", text: "from " },
        { kind: "placeholder", text: "{{MMM}}" },
        { kind: "text", text: " to " },
        { kind: "placeholder", text: "{{DD}}" },
      ],
    ],
    ["Deploy \\{{name}} safely", [{ kind: "text", text: "Deploy \\{{name}} safely" }]],
    ["Test {{MMM DD", [{ kind: "text", text: "Test {{MMM DD" }]],
    [
      "{{YYYY}}{{MM}}",
      [
        { kind: "placeholder", text: "{{YYYY}}" },
        { kind: "placeholder", text: "{{MM}}" },
      ],
    ],
    ["{{yyyy}}", [{ kind: "placeholder", text: "{{yyyy}}" }]],
  ])("splits %s", (value, expected) => {
    expect(splitNameTemplate(value)).toEqual(expected);
  });
});

describe("TemplateNameInput", () => {
  it("moves the backdrop text by the full input scroll offset", () => {
    render(
      <TemplateNameInput
        aria-label="Name"
        value={`Long automation name ${"weekly release ".repeat(16)}{{MMM DD}}`}
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
    const backdrop = screen.getByTestId("task-name-template-overlay");
    const backdropText = backdrop.firstElementChild as HTMLElement;

    input.scrollLeft = 1237;
    fireEvent.scroll(input);

    expect(backdropText.style.transform).toBe("translateX(-1237px)");
  });

  it("resyncs the backdrop after the value changes without a scroll event", () => {
    const initialValue = `Long automation name ${"weekly release ".repeat(16)}{{MMM DD}}`;
    const { rerender } = render(
      <TemplateNameInput aria-label="Name" value={initialValue} onChange={vi.fn()} />,
    );

    const input = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
    const backdropText = screen.getByTestId("task-name-template-overlay")
      .firstElementChild as HTMLElement;

    input.scrollLeft = 1237;
    rerender(
      <TemplateNameInput aria-label="Name" value={`${initialValue} updated`} onChange={vi.fn()} />,
    );

    expect(backdropText.style.transform).toBe("translateX(-1237px)");
  });

  it.each(["First\nSecond {{YYYY}}", "First\rSecond {{YYYY}}", "First {{YYYY\nMM}} Second"])(
    "keeps the backdrop aligned with the native single-line value (%s)",
    (name) => {
      render(<TemplateNameInput aria-label="Name" value={name} onChange={vi.fn()} />);

      const input = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
      const overlay = screen.getByTestId("task-name-template-overlay");

      expect(overlay.textContent).toBe(input.value);
    },
  );
});
