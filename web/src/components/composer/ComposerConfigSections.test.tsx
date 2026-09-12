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

  it("renders the same section framework for the genuinely-different chat and landing inputs (parity)", () => {
    // Feed the DIFFERENT data each real adapter supplies — chat: a "Default" +
    // a disabled "(current)" row; landing: a search leading slot + "Harness
    // default" + a wrapping model row — and assert both still produce the SAME
    // shared framework: a Models section (header + checkbox rows) and a
    // separator-led Effort section. This is the guard against the two composers
    // drifting back into separate page-local config menus (not a same-fixture
    // tautology).
    const assertFramework = (modelsTestId: string, effortTestId: string) => {
      const models = screen.getByTestId(modelsTestId);
      expect(within(models).getByText("Models")).toBeInTheDocument();
      expect(within(models).getAllByRole("menuitemcheckbox").length).toBeGreaterThan(0);
      const efforts = screen.getByTestId(effortTestId);
      expect(within(efforts).getByText("Effort")).toBeInTheDocument();
      expect(within(efforts).getAllByRole("menuitemcheckbox").length).toBeGreaterThan(0);
    };

    // Chat adapter shape (ChatPage.configContent).
    const { unmount } = renderSections({
      models: {
        testId: "composer-agent-models",
        header: "Models",
        choices: [
          { key: "__default__", label: "Default", checked: true, onSelect: vi.fn() },
          { key: "opus", label: "Opus 4.8", checked: false, onSelect: vi.fn() },
          { key: "__current__", label: "Opus 4.8 (current)", checked: false, disabled: true },
        ],
      },
      efforts: {
        testId: "composer-agent-efforts",
        header: "Effort",
        choices: [
          { key: "default", label: "Default", checked: true, onSelect: vi.fn() },
          { key: "high", label: "High", checked: false, onSelect: vi.fn() },
        ],
      },
    });
    assertFramework("composer-agent-models", "composer-agent-efforts");
    unmount();

    // Landing adapter shape (NewChatDialog.selectedConfigContent): a search
    // leading slot + "Harness default" + a wrapping model row.
    renderSections({
      models: {
        testId: "new-chat-landing-agent-models",
        header: "Models",
        leading: <input aria-label="Search models" />,
        choices: [
          { key: "__default__", label: "Harness default", checked: true, onSelect: vi.fn() },
          {
            key: "opus",
            label: "Opus 4.8 (1M context)",
            checked: false,
            onSelect: vi.fn(),
            title: "Opus 4.8 (1M context)",
            className: "whitespace-normal break-words",
          },
        ],
      },
      efforts: {
        testId: "new-chat-landing-agent-efforts",
        header: "Effort",
        choices: [
          { key: "default", label: "Default", checked: true, onSelect: vi.fn() },
          { key: "high", label: "High", checked: false, onSelect: vi.fn() },
        ],
      },
    });
    assertFramework("new-chat-landing-agent-models", "new-chat-landing-agent-efforts");
    // The landing's page-local search slot renders inside the shared section.
    expect(
      within(screen.getByTestId("new-chat-landing-agent-models")).getByLabelText("Search models"),
    ).toBeInTheDocument();
  });
});
