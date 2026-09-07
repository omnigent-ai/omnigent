import type { Meta, StoryObj } from "@storybook/react-vite";
import { TerminalCommandCard } from "./TerminalCommandCard";

const meta = {
  title: "Components/Blocks/TerminalCommandCard",
  component: TerminalCommandCard,
  tags: ["visual-snapshot"],
} satisfies Meta<typeof TerminalCommandCard>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Input: Story = {
  args: {
    kind: "input",
    input: "pnpm --filter web run type-check",
    stdout: null,
    stderr: null,
  },
};

export const OutputAvailable: Story = {
  args: {
    kind: "output",
    input: null,
    stdout: "Typescript checks passed.\n",
    stderr: null,
  },
};

export const StandardError: Story = {
  args: {
    kind: "output",
    input: null,
    stdout: null,
    stderr: "error TS2322: Type 'string' is not assignable to type 'number'.\n",
  },
};

export const NoOutput: Story = {
  args: {
    kind: "output",
    input: null,
    stdout: null,
    stderr: null,
  },
};
