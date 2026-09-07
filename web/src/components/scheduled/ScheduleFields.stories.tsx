import type { Meta, StoryObj } from "@storybook/react-vite";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import { DEFAULT_SCHEDULE_MODEL, type ScheduleModel } from "@/lib/scheduleBuilder";
import { ScheduleFields } from "./ScheduleFields";

const meta = {
  title: "Components/Scheduled/ScheduleFields",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

function ScheduleExample({ initialModel }: { initialModel: ScheduleModel }) {
  const [model, setModel] = useState(initialModel);
  return (
    <div className="w-[560px] rounded-xl border bg-card p-4">
      <ScheduleFields model={model} onChange={setModel} />
    </div>
  );
}

export const HourlyMinute: Story = {
  render: () => (
    <ScheduleExample initialModel={{ ...DEFAULT_SCHEDULE_MODEL, preset: "hourly", minute: 15 }} />
  ),
};

export const WeeklyTimePickerOpen: Story = {
  render: () => (
    <ScheduleExample
      initialModel={{
        ...DEFAULT_SCHEDULE_MODEL,
        preset: "weekly",
        hour: 14,
        minute: 30,
        weekdays: ["MO", "WE", "FR"],
      }}
    />
  ),
  play: async ({ canvasElement }) => {
    await userEvent.click(within(canvasElement).getByTestId("schedule-time-picker-trigger"));
  },
};
