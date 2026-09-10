import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import { Badge } from "@/components/ui/badge";
import { ModelValueCombobox } from "./ModelValueCombobox";

const options = [
  "databricks-claude-opus-4-8",
  "databricks-claude-sonnet-5",
  "databricks-claude-haiku-4-5",
  "openai-gpt-5-4",
];

const noModels: string[] = [];

const meta = {
  title: "Components/Controls/ModelValueCombobox",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

function ComboboxExample({ initialSelected = noModels }: { initialSelected?: string[] }) {
  const [selected, setSelected] = useState(initialSelected);
  const toggle = (value: string) =>
    setSelected((current) =>
      current.includes(value) ? current.filter((item) => item !== value) : [...current, value],
    );

  return (
    <div className="w-[420px] space-y-2">
      <ModelValueCombobox options={options} selected={selected} onToggle={toggle} />
      <div className="flex min-h-6 flex-wrap gap-1.5" data-testid="story-selected-models">
        {selected.map((model) => (
          <Badge key={model} variant="secondary">
            {model}
          </Badge>
        ))}
      </div>
    </div>
  );
}

export const OpenWithSelections: Story = {
  render: () => <ComboboxExample initialSelected={["databricks-claude-sonnet-5"]} />,
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByRole("textbox"));
  },
};

export const FilteredResults: Story = {
  render: () => <ComboboxExample initialSelected={["databricks-claude-sonnet-5"]} />,
  play: async ({ canvasElement }) => {
    await userEvent.type(within(canvasElement).getByRole("textbox"), "opus");
  },
};

export const FreeFormValueAdded: Story = {
  render: () => <ComboboxExample />,
  play: async ({ canvasElement }) => {
    const input = within(canvasElement).getByRole("textbox");
    await userEvent.type(input, "custom-model-v2");
    await userEvent.keyboard("{Enter}");
  },
};
