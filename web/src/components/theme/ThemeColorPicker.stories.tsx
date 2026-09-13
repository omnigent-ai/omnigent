import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import { ThemeColorPicker } from "./ThemeColorPicker";

const meta = {
  title: "Components/Theme/ThemeColorPicker",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

function PickerExample({ initialValue = "#e52671" }: { initialValue?: string }) {
  const [value, setValue] = useState(initialValue);
  return (
    <div className="w-[480px] rounded-xl border bg-card px-4">
      <ThemeColorPicker
        label="Accent color"
        value={value}
        testId="story-accent"
        onChange={setValue}
      />
    </div>
  );
}

export const ClosedSwatch: Story = {
  render: () => <PickerExample />,
};

export const OpenWithEditedHex: Story = {
  name: "Open green palette",
  render: () => <PickerExample initialValue="#34c759" />,
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("story-accent-trigger"));
  },
};
