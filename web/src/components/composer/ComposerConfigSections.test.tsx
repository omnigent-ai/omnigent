import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ComposerConfigSections,
  type ComposerConfigChoice,
  type ComposerConfigSection,
} from "./ComposerConfigSections";
import { DropdownMenu, DropdownMenuContent } from "@/components/ui/dropdown-menu";
import { nativeModelLabel } from "@/lib/composerModelLabel";

afterEach(cleanup);

// The checkbox rows need a menu context; render the sections inside an open
// menu, the same way both harness pickers mount them.
function renderSections(props: {
  sdk?: ComposerConfigSection;
  models?: ComposerConfigSection;
  efforts?: ComposerConfigSection;
  extra?: ComposerConfigSection[];
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
  it("renders Agent SDK before Models with separators between sections", () => {
    renderSections({
      sdk: {
        testId: "sdks",
        header: "Agent SDK",
        choices: [
          { key: "claude", label: "Claude SDK", checked: true, onSelect: vi.fn() },
          { key: "codex", label: "Codex", checked: false, onSelect: vi.fn() },
        ],
      },
      models: modelsSection("models"),
    });

    const sdk = screen.getByTestId("sdks");
    const models = screen.getByTestId("models");
    expect(within(sdk).getByText("Agent SDK")).toBeInTheDocument();
    expect(sdk.compareDocumentPosition(models) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByRole("separator")).toHaveLength(1);
  });

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

  it("omits the leading separator for a standalone effort submenu", () => {
    renderSections({
      efforts: {
        testId: "efforts",
        header: "Effort",
        choices: [{ key: "high", label: "High", checked: true }],
      },
    });
    expect(screen.getByRole("menuitemcheckbox", { name: "High" })).toBeVisible();
    expect(screen.queryByRole("separator")).toBeNull();
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

  it("renders extra sections after Models/Effort, each with its own header and separator-led row", () => {
    renderSections({
      models: modelsSection("models"),
      extra: [
        {
          testId: "fusion-leads",
          header: "Lead",
          choices: [{ key: "fable", label: "Claude Fable 5.1", checked: true, onSelect: vi.fn() }],
        },
        {
          testId: "fusion-sidekicks",
          header: "Sidekick",
          choices: [{ key: "swe", label: "SWE-2 Medium", checked: true, onSelect: vi.fn() }],
        },
      ],
    });
    expect(within(screen.getByTestId("fusion-leads")).getByText("Lead")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("fusion-sidekicks")).getByText("SWE-2 Medium"),
    ).toBeInTheDocument();
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

// A Pi-style mixed catalog: Claude from two hosts, GPT, Gemini, GLM, Kimi, and
// an alias without creator metadata.
const MIXED_CATALOG = [
  { id: "anthropic/claude-opus-5", displayName: "Claude Opus 5" },
  { id: "anthropic/claude-sonnet-5", displayName: "Claude Sonnet 5" },
  { id: "amazon-bedrock/us.anthropic.claude-sonnet-5", displayName: "Claude Sonnet 5" },
  { id: "openai/gpt-5.5", displayName: "GPT-5.5" },
  { id: "openai/gpt-5.5-mini", displayName: "GPT-5.5 mini" },
  { id: "openrouter/google/gemini-3-pro", displayName: "Gemini 3 Pro" },
  { id: "omnigent-openai/system.ai.glm-5-2", displayName: "GLM-5.2" },
  { id: "omnigent/databricks-kimi-k2-7-code", displayName: "Kimi K2.7 Code" },
  { id: "groq/llama-3.3-70b-versatile", displayName: "Llama 3.3 70B" },
  { id: "radius/balanced", displayName: "Balanced" },
];

function catalogSection(
  catalog: readonly { id: string; displayName: string }[],
  selected: string,
  onSelect = vi.fn(),
): ComposerConfigSection {
  const modelChoice = (option: { id: string; displayName: string }): ComposerConfigChoice => ({
    key: option.id,
    label: nativeModelLabel(option),
    checked: option.id === selected,
    onSelect: () => onSelect(option.id),
    testId: `models-${option.id}`,
    modelOption: option,
  });
  return {
    testId: "models",
    header: "Models",
    choices: [
      { key: "__default__", label: "Default", checked: selected === "", onSelect: vi.fn() },
      ...catalog.map(modelChoice),
    ],
  };
}

const groupToggle = (label: string) => screen.getByRole("menuitem", { name: new RegExp(label) });
const modelRow = (name: string) => screen.queryByRole("menuitemcheckbox", { name });

describe("model catalog browsing", () => {
  it("groups a long mixed catalog by creator with the selected group expanded", () => {
    renderSections({ models: catalogSection(MIXED_CATALOG, "openai/gpt-5.5") });

    expect(
      screen.getAllByRole("menuitem").map((header) => [header.textContent, header.ariaExpanded]),
    ).toEqual([
      ["Anthropic3", "false"],
      ["OpenAI2", "true"],
      ["Google1", "false"],
      ["Moonshot AI1", "false"],
      ["Z.ai1", "false"],
      ["Meta1", "false"],
      ["Other models1", "false"],
    ]);
    expect(modelRow("GPT-5.5")).toHaveAttribute("aria-checked", "true");
    expect(modelRow("Claude Opus 5")).toBeNull();
    expect(modelRow("Default")).toBeVisible();

    fireEvent.click(groupToggle("Anthropic"));
    expect(groupToggle("Anthropic")).toHaveAttribute("aria-expanded", "true");
    // Same-named rows from two hosts show which host serves each.
    expect(modelRow("Claude Sonnet 5 anthropic")).toBeVisible();
    expect(modelRow("Claude Sonnet 5 amazon-bedrock")).toBeVisible();
    fireEvent.click(groupToggle("OpenAI"));
    expect(modelRow("GPT-5.5")).toBeNull();
  });

  it("scrolls the selected model into view once when the menu opens", () => {
    const scrollIntoView = vi.spyOn(Element.prototype, "scrollIntoView");
    try {
      const { rerender } = renderSections({
        models: catalogSection(MIXED_CATALOG, "omnigent/databricks-kimi-k2-7-code"),
      });
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
      expect(scrollIntoView.mock.contexts[0]).toBe(modelRow("Kimi K2.7 Code"));
      rerender(
        <DropdownMenu open>
          <DropdownMenuContent>
            <ComposerConfigSections models={catalogSection(MIXED_CATALOG, "openai/gpt-5.5")} />
          </DropdownMenuContent>
        </DropdownMenu>,
      );
      expect(scrollIntoView).toHaveBeenCalledTimes(1);
    } finally {
      scrollIntoView.mockRestore();
    }
  });

  it("searches names, ids, and creators across collapsed groups", () => {
    const onSelect = vi.fn();
    renderSections({ models: catalogSection(MIXED_CATALOG, "openai/gpt-5.5", onSelect) });
    const search = screen.getByRole("textbox", { name: "Search models" });

    fireEvent.change(search, { target: { value: "moonshot" } });
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    expect(screen.getByRole("group", { name: "Moonshot AI" })).toHaveTextContent("Kimi K2.7 Code");
    expect(modelRow("GPT-5.5")).toBeNull();
    expect(modelRow("Default")).toBeVisible();

    fireEvent.change(search, { target: { value: "system.ai.glm" } });
    fireEvent.click(modelRow("GLM-5.2")!);
    expect(onSelect).toHaveBeenCalledWith("omnigent-openai/system.ai.glm-5-2");

    fireEvent.change(search, { target: { value: "no such model" } });
    expect(screen.getByRole("status")).toHaveTextContent("No matching models");

    fireEvent.change(search, { target: { value: "" } });
    expect(groupToggle("OpenAI")).toHaveAttribute("aria-expanded", "true");
    expect(groupToggle("Moonshot AI")).toHaveAttribute("aria-expanded", "false");
  });

  it("moves between search and rows with the arrow keys and toggles groups with Enter", () => {
    renderSections({ models: catalogSection(MIXED_CATALOG, "") });
    const search = screen.getByRole("textbox", { name: "Search models" });

    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(modelRow("Default")).toHaveFocus();
    fireEvent.keyDown(modelRow("Default")!, { key: "ArrowUp" });
    expect(search).toHaveFocus();

    fireEvent.keyDown(groupToggle("Google"), { key: "Enter" });
    expect(modelRow("Gemini 3 Pro")).toBeVisible();
  });

  it("keeps small or single-creator catalogs simple", () => {
    const { unmount } = renderSections({
      models: catalogSection(MIXED_CATALOG.slice(3, 8), "openai/gpt-5.5"),
    });
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(6);
    unmount();

    const claude = [..."abcdefghij"].map((suffix) => ({
      id: `anthropic/claude-${suffix}`,
      displayName: `Claude ${suffix}`,
    }));
    renderSections({ models: catalogSection(claude, "anthropic/claude-a") });
    expect(screen.getByRole("textbox", { name: "Search models" })).toBeVisible();
    expect(screen.queryAllByRole("menuitem")).toHaveLength(0);
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(11);
  });

  it("renders a cycle preview flat", () => {
    render(
      <DropdownMenu open>
        <DropdownMenuContent>
          <ComposerConfigSections
            models={catalogSection(MIXED_CATALOG, "openai/gpt-5.5")}
            browseModels={false}
          />
        </DropdownMenuContent>
      </DropdownMenu>,
    );
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.getAllByRole("menuitemcheckbox")).toHaveLength(MIXED_CATALOG.length + 1);
  });
});
