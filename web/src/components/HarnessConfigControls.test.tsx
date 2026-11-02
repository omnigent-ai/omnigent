import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RoutingModelSelect, type RoutingModelOption } from "@/components/HarnessConfigControls";

afterEach(cleanup);

function openModelSelect(models: readonly RoutingModelOption[]): void {
  render(
    <RoutingModelSelect
      value="__default__"
      onValueChange={vi.fn()}
      offerSmartRouting={false}
      testId="model-select"
      models={models}
    />,
  );
  const trigger = screen.getByTestId("model-select");
  fireEvent.pointerDown(trigger, new MouseEvent("pointerdown", { bubbles: true, button: 0 }));
  fireEvent.click(trigger);
}

const PI_MODELS: RoutingModelOption[] = [
  "anthropic/claude-opus-5",
  "anthropic/claude-sonnet-5",
  "amazon-bedrock/us.anthropic.claude-haiku-5-5",
  "openai/gpt-5.5",
  "openai/gpt-5.5-mini",
  "google/gemini-3-pro",
  "zai/glm-5.2",
  "moonshotai/kimi-k3",
  "radius/balanced",
  "radius/cheap",
].map((id) => ({ id, label: id.split("/").pop()! }));

describe("RoutingModelSelect", () => {
  it("heads a long mixed catalog by creator", () => {
    openModelSelect(PI_MODELS);
    const groupOptions = (name: string) =>
      within(screen.getByRole("group", { name }))
        .getAllByRole("option")
        .map((option) => option.textContent);
    expect(screen.getAllByRole("group").map((group) => group.firstChild?.textContent)).toEqual([
      "Anthropic",
      "OpenAI",
      "Google",
      "Moonshot AI",
      "Z.ai",
      "Other models",
    ]);
    expect(groupOptions("Anthropic")).toEqual([
      "claude-opus-5",
      "claude-sonnet-5",
      "us.anthropic.claude-haiku-5-5",
    ]);
    expect(groupOptions("Other models")).toEqual(["balanced", "cheap"]);
  });

  it("keeps a short catalog flat", () => {
    openModelSelect(PI_MODELS.slice(0, 4));
    expect(screen.queryAllByRole("group")).toHaveLength(0);
    expect(screen.getAllByRole("option")).toHaveLength(5);
  });
});
