import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";

import { type DiffReviewBase, DiffReviewBaseMenu } from "./DiffReviewBaseMenu";

const choice = (id: string, label: string, description?: string) => ({
  id,
  label,
  ...(description ? { description } : {}),
  checked: id === "head",
  unavailableReason: null,
  onSelect: vi.fn(),
});

function openMenu(base: DiffReviewBase) {
  render(
    <TooltipProvider>
      <DiffReviewBaseMenu base={base} />
    </TooltipProvider>,
  );
  fireEvent.pointerDown(screen.getByTestId("diff-review-base"), { button: 0 });
  return screen.getByRole("menu");
}

describe("DiffReviewBaseMenu", () => {
  const base = { label: "vs HEAD", description: "Compared with the last commit." };

  it("keeps its default heading, and choices without a description show their label only", () => {
    const menu = openMenu({ ...base, choices: [choice("head", "Last commit")] });
    expect(menu).toHaveTextContent("Compare changes with");
    expect(screen.getByRole("menuitemradio", { name: "Last commit" })).toHaveTextContent(
      /^Last commit$/,
    );
  });

  it("shows a source's own heading and each choice's description", () => {
    const menu = openMenu({
      ...base,
      heading: "Review changes against",
      choices: [
        choice("head", "Last commit"),
        choice("base", "Base branch", "Compares with main."),
      ],
    });
    expect(menu).toHaveTextContent("Review changes against");
    expect(menu).not.toHaveTextContent("Compare changes with");
    expect(screen.getByRole("menuitemradio", { name: /Base branch/ })).toHaveTextContent(
      "Compares with main.",
    );
  });
});
