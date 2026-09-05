import type { Meta, StoryObj } from "@storybook/react-vite";
import { userEvent } from "storybook/test";
import { ToolCard } from "./ToolCard";

const longOutput = Array.from(
  { length: 85 },
  (_, index) => `Line ${String(index + 1).padStart(2, "0")}: deterministic tool output`,
).join("\n");

const meta = {
  title: "Components/Blocks/ToolCard",
  component: ToolCard,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof ToolCard>;

export default meta;
type Story = StoryObj<typeof meta>;

async function expand(canvasElement: HTMLElement): Promise<void> {
  const trigger = canvasElement.querySelector<HTMLElement>('[data-slot="collapsible-trigger"]');
  if (!trigger) throw new Error("Tool card trigger not found");
  await userEvent.click(trigger);
}

export const CompletedFileRead: Story = {
  args: {
    name: "sys_os_read",
    arguments: { path: "src/components/blocks/ToolCard.tsx" },
    output: "File read successfully.",
    state: "output-available",
    startedAt: null,
    duration: 3.25,
  },
};

export const LongOutputPreview: Story = {
  args: {
    name: "component_audit",
    argsSummary: "mode=visual",
    arguments: { mode: "visual", requestedLines: 85 },
    output: longOutput,
    state: "output-available",
    startedAt: null,
    duration: 1.2,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const RunningWithPinnedDuration: Story = {
  args: {
    name: "web_search",
    arguments: { query: "deterministic component states" },
    output: null,
    state: "input-available",
    startedAt: null,
    duration: 2.5,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};

export const FailedExpanded: Story = {
  args: {
    name: "sys_os_shell",
    arguments: { command: "exit 1" },
    output: null,
    state: "output-error",
    startedAt: null,
    duration: 0.042,
  },
  play: async ({ canvasElement }) => expand(canvasElement),
};
