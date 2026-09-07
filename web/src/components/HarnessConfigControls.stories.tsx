import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import {
  ConfigRow,
  DescribedSelect,
  EFFORT_UNAVAILABLE_PLACEHOLDER,
  MODEL_SELECT_DEFAULT,
  MODEL_SELECT_SMART,
  RoutingModelSelect,
} from "./HarnessConfigControls";

const models = [
  { id: "sonnet", label: "Sonnet 5" },
  { id: "opus", label: "Opus 4.10" },
  { id: "haiku", label: "Haiku 4.5" },
];

const permissionOptions = [
  { value: "default", label: "Default", description: "Use the agent's configured permissions." },
  { value: "ask", label: "Ask", description: "Ask before file edits and shell commands." },
  { value: "auto", label: "Auto", description: "Allow supported operations for this session." },
];

const effortOptions = [
  { value: "none", label: EFFORT_UNAVAILABLE_PLACEHOLDER, description: "No effort override." },
  { value: "medium", label: "Medium", description: "Balanced reasoning for most tasks." },
  { value: "high", label: "High", description: "More reasoning for complex tasks." },
];

const meta = {
  title: "Components/Controls/HarnessConfigControls",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

function ModelSelectExample() {
  const [value, setValue] = useState(MODEL_SELECT_DEFAULT);
  return (
    <div className="w-[480px]">
      <ConfigRow label="Model" description="Choose a model or let Smart Routing decide.">
        <RoutingModelSelect
          value={value}
          onValueChange={setValue}
          offerSmartRouting
          testId="story-model-select"
          models={models}
          defaultLabel="Default (Sonnet 5)"
          activeModelId="sonnet"
        />
      </ConfigRow>
    </div>
  );
}

export const ModelMenuOpen: Story = {
  render: () => <ModelSelectExample />,
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("story-model-select"));
  },
};

function PermissionSelectExample() {
  const [value, setValue] = useState("default");
  return (
    <div className="w-[480px]">
      <ConfigRow label="Permissions" description="Control what the agent can do automatically.">
        <DescribedSelect
          value={value}
          onValueChange={setValue}
          options={permissionOptions}
          testId="story-permission-select"
          ariaLabel="Permissions"
        />
      </ConfigRow>
    </div>
  );
}

export const PermissionDetailOpen: Story = {
  render: () => <PermissionSelectExample />,
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("story-permission-select"));
    await userEvent.hover(
      within(canvasElement.ownerDocument.body).getByRole("option", { name: "Auto" }),
    );
  },
};

export const SmartRoutingLocksEffort: Story = {
  render: () => (
    <div className="flex w-[480px] flex-col gap-5">
      <ConfigRow label="Model" description="Smart Routing chooses a model for each turn.">
        <RoutingModelSelect
          value={MODEL_SELECT_SMART}
          onValueChange={() => undefined}
          offerSmartRouting
          testId="story-smart-model"
          models={models}
        />
      </ConfigRow>
      <ConfigRow label="Effort" description="Unavailable when the model changes per turn.">
        <DescribedSelect
          value="none"
          onValueChange={() => undefined}
          options={effortOptions}
          testId="story-effort-select"
          ariaLabel="Effort"
          disabled
        />
      </ConfigRow>
    </div>
  ),
};
