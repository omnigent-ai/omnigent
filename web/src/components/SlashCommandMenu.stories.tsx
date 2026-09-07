import type { Meta, StoryObj } from "@storybook/react-vite";
import { BUILTIN_SLASH_COMMANDS, SlashCommandMenu } from "./SlashCommandMenu";

const commands = {
  ...BUILTIN_SLASH_COMMANDS,
  "/review": "Review the current change for correctness, maintainability, and test coverage.",
  "/simplify": "Make recently changed code easier to understand without changing behavior.",
  "/test": "Run focused tests and explain any failures.",
};

const meta = {
  title: "Components/Menus/SlashCommandMenu",
  component: SlashCommandMenu,
  tags: ["visual-snapshot"],
  args: {
    commands,
    onSelect: () => undefined,
  },
  decorators: [
    (Story) => (
      <div className="relative h-[420px] w-[640px] rounded-xl border bg-card">
        <div className="absolute inset-x-4 bottom-4 h-12 rounded-xl border bg-background">
          <Story />
        </div>
      </div>
    ),
  ],
} satisfies Meta<typeof SlashCommandMenu>;

export default meta;
type Story = StoryObj<typeof meta>;

export const CommandsAndSkills: Story = {
  args: {
    query: "",
    activeIndex: 0,
  },
};

export const ActiveSkill: Story = {
  args: {
    query: "",
    activeIndex: Object.keys(BUILTIN_SLASH_COMMANDS).length,
  },
};

export const FilteredSkill: Story = {
  args: {
    query: "rev",
    activeIndex: 0,
  },
};
