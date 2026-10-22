import type { Meta, StoryObj } from "@storybook/react-vite";
import { SlashCommandCard } from "./SlashCommandCard";

const meta = {
  title: "Components/Blocks/SlashCommandCard",
  component: SlashCommandCard,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof SlashCommandCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Skill: Story = {
  args: {
    kind: "skill",
    name: "review",
    arguments: "",
    output: null,
  },
};

export const SkillWithArguments: Story = {
  args: {
    kind: "skill",
    name: "simplify",
    arguments: "web/src/components",
    output: null,
  },
};

export const Command: Story = {
  args: {
    kind: "command",
    name: "model",
    arguments: "sonnet",
    output: "Model changed to Sonnet.",
  },
};
