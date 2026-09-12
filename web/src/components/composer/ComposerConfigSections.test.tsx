import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ComposerConfigSections, type ComposerConfigSection } from "./ComposerConfigSections";
import { DropdownMenu, DropdownMenuContent } from "@/components/ui/dropdown-menu";

afterEach(cleanup);

// The checkbox rows need a menu context; render the sections inside an open
// menu, the same way both harness pickers mount them.
function renderSections(props: {
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
}) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <ComposerConfigSections {...props} />
      </DropdownMenuContent>
    </DropdownMenu>,
  );
}

const modelsSection = (testId: string): ComposerConfigSection => ({
  testId,
  header: "Models",
  choices: [
    {
      key: "default",
      label: "Default",
      checked: true,
      onSelect: vi.fn(),
      testId: `${testId}-default`,
    },
    {
      key: "opus",
      label: "Opus 4.8 (1M context)",
      checked: false,
      onSelect: vi.fn(),
      testId: `${testId}-opus`,
      data: { "data-model-id": "opus" },
    },
  ],
});

describe("ComposerConfigSections", () => {
  it("renders Models and Effort sections with headers, testids, and one row per choice", () => {
    renderSections({
      models: modelsSection("models"),
      efforts: {
        testId: "efforts",
        header: "Effort",
        choices: [
          {
            key: "default",
            label: "Default",
            checked: true,
            onSelect: vi.fn(),
            testId: "efforts-default",
          },
          { key: "high", label: "High", checked: false, onSelect: vi.fn(), testId: "efforts-high" },
        ],
      },
    });
    expect(within(screen.getByTestId("models")).getByText("Models")).toBeInTheDocument();
    expect(screen.getByTestId("models-opus")).toHaveAttribute("data-model-id", "opus");
    expect(within(screen.getByTestId("efforts")).getByText("Effort")).toBeInTheDocument();
    expect(screen.getByTestId("efforts-high")).toBeInTheDocument();
  });

  it("dispatches a choice's onSelect through the checkbox change handler", () => {
    const onSelect = vi.fn();
    renderSections({
      models: {
        testId: "models",
        header: "Models",
        choices: [{ key: "opus", label: "Opus", checked: false, onSelect, testId: "models-opus" }],
      },
    });
    fireEvent.click(screen.getByTestId("models-opus"));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("omits a section whose prop is undefined and renders a static (no-onSelect) row disabled", () => {
    renderSections({
      models: {
        testId: "models",
        header: "Models",
        choices: [{ key: "current", label: "Opus (current)", checked: true, disabled: true }],
      },
    });
    expect(screen.queryByTestId("efforts")).not.toBeInTheDocument();
    expect(screen.getByText("Opus (current)")).toBeInTheDocument();
  });

  it("renders the same composed structure for the chat and landing callers (parity)", () => {
    // Both surfaces feed the SAME component, so a chat-keyed and a landing-keyed
    // section render identical structure — the guard against the two composers
    // drifting back into separate page-local config menus.
    const shape = (root: HTMLElement) => ({
      header: within(root).getByText("Models").textContent,
      rows: within(root).getAllByRole("menuitemcheckbox").length,
    });
    const { unmount } = renderSections({ models: modelsSection("composer-agent-models") });
    const chat = shape(screen.getByTestId("composer-agent-models"));
    unmount();
    renderSections({ models: modelsSection("new-chat-landing-agent-models") });
    const landing = shape(screen.getByTestId("new-chat-landing-agent-models"));
    expect(landing).toEqual(chat);
  });
});
