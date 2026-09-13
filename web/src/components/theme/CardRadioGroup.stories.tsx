import type { Meta, StoryObj } from "@storybook/react-vite";
import { MonitorIcon, SunIcon } from "lucide-react";
import { useState } from "react";
import { userEvent, within } from "storybook/test";
import { PALETTES } from "@/lib/themePalette";
import {
  DARK_MODE_PREVIEW,
  LIGHT_MODE_PREVIEW,
  ModePreview,
  PaletteChip,
  PaletteSwatchPreview,
} from "./AppearancePreviews";
import { CardRadioGroup, type CardRadioOption } from "./CardRadioGroup";
import type { ThemeMode } from "./themeMode";

const modeItems: CardRadioOption<ThemeMode>[] = [
  {
    value: "system",
    testId: "story-theme-system",
    body: (
      <>
        <ModePreview variant="system" />
        <span className="text-ui font-medium">System</span>
      </>
    ),
  },
  {
    value: "light",
    testId: "story-theme-light",
    body: (
      <>
        <ModePreview variant="light" />
        <span className="text-ui font-medium">Light</span>
      </>
    ),
  },
  {
    value: "dark",
    testId: "story-theme-dark",
    body: (
      <>
        <ModePreview variant="dark" />
        <span className="text-ui font-medium">Dark</span>
      </>
    ),
  },
];

function ModeGroup({ initialValue }: { initialValue: ThemeMode }) {
  const [value, setValue] = useState(initialValue);
  return (
    <div className="w-[700px] space-y-3 rounded-xl border bg-card p-4">
      <h2 id="story-mode-label" className="font-medium">
        Appearance mode
      </h2>
      <CardRadioGroup
        labelledBy="story-mode-label"
        value={value}
        onSelect={setValue}
        items={modeItems}
        className="grid grid-cols-3 gap-3"
        cardClassName="gap-2 p-2"
      />
    </div>
  );
}

const meta = {
  title: "Components/Theme/CardRadioGroup",
  tags: ["visual-snapshot"],
} satisfies Meta;

export default meta;
type Story = StoryObj<typeof meta>;

export const SystemSelected: Story = { render: () => <ModeGroup initialValue="system" /> };
export const DarkSelected: Story = { render: () => <ModeGroup initialValue="dark" /> };

export const TwoColumnIconCards: Story = {
  render: () => (
    <div className="w-[560px] space-y-3 rounded-xl border bg-card p-4">
      <h2 id="story-terminal-theme" className="font-medium">
        Terminal theme
      </h2>
      <CardRadioGroup
        labelledBy="story-terminal-theme"
        value="auto"
        onSelect={() => undefined}
        className="grid grid-cols-2 gap-3"
        cardClassName="items-center gap-2 p-4"
        items={[
          {
            value: "auto",
            testId: "story-terminal-auto",
            body: (
              <>
                <MonitorIcon className="size-6 text-muted-foreground" />
                <span>Match app</span>
              </>
            ),
          },
          {
            value: "light",
            testId: "story-terminal-light",
            body: (
              <>
                <SunIcon className="size-6 text-muted-foreground" />
                <span>Light</span>
              </>
            ),
          },
        ]}
      />
    </div>
  ),
};

export const ArrowKeyMovesSelection: Story = {
  render: () => <ModeGroup initialValue="system" />,
  play: async ({ canvasElement }) => {
    const selected = within(canvasElement).getByTestId("story-theme-system");
    selected.focus();
    await userEvent.keyboard("{ArrowRight}");
  },
};

export const PreviewPrimitives: Story = {
  render: () => (
    <div className="grid w-[700px] grid-cols-3 gap-4 rounded-xl border bg-card p-4">
      <ModePreview variant="system" />
      <ModePreview variant="light" />
      <ModePreview variant="dark" />
      <PaletteSwatchPreview swatch={PALETTES[0]!.light} />
      <div className="flex items-center gap-3 rounded-lg border p-3">
        <PaletteChip swatch={LIGHT_MODE_PREVIEW} />
        <span>Light palette</span>
      </div>
      <div className="flex items-center gap-3 rounded-lg border p-3">
        <PaletteChip swatch={DARK_MODE_PREVIEW} />
        <span>Dark palette</span>
      </div>
    </div>
  ),
};
