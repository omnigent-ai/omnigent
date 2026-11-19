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

// Utilities that place glyphs: padding, border width, font, size and line height.
const TEXT_LAYOUT_CLASS =
  /^(md:)?(p[xytrbl]?-|border(-\d+)?$|text-(ui|message|\[)|font-|leading-|tracking-)/;

function textLayoutClasses(element: Element) {
  return [...element.classList].filter((name) => TEXT_LAYOUT_CLASS.test(name)).sort();
}

describe("TemplateNameInput", () => {
  it.each([
    ["the default input metrics", undefined],
    [
      "a heading-style caller className",
      "h-8 border-0 px-0 py-0 pr-10 font-heading text-[1.25em] leading-[32px] font-[600] md:text-[1.25em]",
    ],
  ])("lays the backdrop text out like the input with %s", (_label, className) => {
    render(
      <TemplateNameInput
        aria-label="Name"
        className={className}
        value="Nightly {{MMM DD}}"
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Name" });
    const backdrop = screen.getByTestId("task-name-template-overlay");

    expect(textLayoutClasses(backdrop)).toEqual(textLayoutClasses(input));
  });

  it("moves the backdrop text by the full input scroll offset", () => {
    render(
      <TemplateNameInput
        aria-label="Name"
        value={`Long automation name ${"weekly release ".repeat(16)}{{MMM DD}}`}
        onChange={vi.fn()}
      />,
    );

    const input = screen.getByRole("textbox", { name: "Name" }) as HTMLInputElement;
    const backdropText = screen.getByTestId("task-name-template-overlay-text");

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
    const backdropText = screen.getByTestId("task-name-template-overlay-text");

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
