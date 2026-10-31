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

function ScheduleExample({
  initialModel,
  initialTimezone = "Asia/Singapore",
  width = 560,
}: {
  initialModel: ScheduleModel;
  initialTimezone?: string;
  width?: number;
}) {
  const [model, setModel] = useState(initialModel);
  const [timezone, setTimezone] = useState(initialTimezone);
  return (
    <div className="rounded-xl border bg-card p-4" style={{ width }}>
      <ScheduleFields
        model={model}
        anchoredScheduling
        onChange={setModel}
        timezone={timezone}
        onTimezoneChange={setTimezone}
      />
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

export const NarrowCustomSchedule: Story = {
  render: () => (
    <ScheduleExample
      width={340}
      initialTimezone="America/Argentina/Buenos_Aires"
      initialModel={{
        ...DEFAULT_SCHEDULE_MODEL,
        preset: "custom",
        customFreq: "weekly",
        interval: 3,
        weekdays: ["MO", "WE", "FR", "SU"],
        hour: 14,
        minute: 30,
        end: { type: "count", count: 12 },
      }}
    />
  ),
};
