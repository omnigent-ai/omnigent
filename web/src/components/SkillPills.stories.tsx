import type { Meta, StoryObj } from "@storybook/react-vite";
import { SkillPills } from "./SkillPills";

const meta = {
  title: "Components/SkillPills",
  component: SkillPills,
  tags: ["visual-snapshot"],
  args: {
    onPick: () => undefined,
  },
} satisfies Meta<typeof SkillPills>;

export default meta;
type Story = StoryObj<typeof meta>;

export const SingleSkill: Story = {
  args: {
    skills: [{ name: "review", description: "Review the current change for correctness." }],
  },
};

export const SeveralSkills: Story = {
  args: {
    skills: [
      { name: "review", description: "Review the current change for correctness." },
      { name: "test", description: "Run focused tests and explain any failures." },
      { name: "simplify", description: "Make recently changed code easier to understand." },
    ],
  },
};
