import type { Meta, StoryObj } from "@storybook/react-vite";
import type { ReactNode } from "react";
import { within } from "storybook/test";
import { TerminalExtraKeys, type ExtraKeysTarget } from "./TerminalExtraKeys";
import { LONG_PRESS_MS } from "./terminalExtraKeysModel";

const noopTarget: ExtraKeysTarget = {
  send: () => {},
  setTransform: () => {},
  focus: () => {},
  applicationCursor: () => false,
};

/** Widest surface the preview wrapper shows unclipped; wider ones are zoomed down. */
const PREVIEW_MAX_WIDTH = 700;

/**
 * Keyboard-closed framing: the row is the bottom-most element of the
 * terminal surface, docked under a (mock) xterm pane, with no soft keyboard
 * below it. Width is the whole phone / tablet, so the columns widen but the
 * fourteen keys never move or wrap.
 */
function Surface({ width, children }: { width: number; children: ReactNode }) {
  return (
    <div
      style={{ width, zoom: Math.min(1, PREVIEW_MAX_WIDTH / width) }}
      className="flex flex-col overflow-hidden rounded-lg border border-border bg-card shadow-sm"
    >
      <div
        aria-hidden
        className="h-40 bg-zinc-950 p-2 font-mono text-[13px] leading-5 text-zinc-100"
      >
        <div>$ claude</div>
        <div className="text-zinc-400">⏵⏵ accept edits on (shift+tab to cycle)</div>
        <div>
          &gt; <span className="animate-pulse">▍</span>
        </div>
      </div>
      {children}
    </div>
  );
}

const meta = {
  title: "Components/Terminal/TerminalExtraKeys",
  component: TerminalExtraKeys,
  tags: ["visual-snapshot"],
  args: { target: noopTarget },
  parameters: { layout: "padded" },
} satisfies Meta<typeof TerminalExtraKeys>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Press a modifier for ``holdMs`` with raw pointer events — the row acts on
 * pointerdown/pointerup, and pointerId 1 is the browser's always-present mouse.
 */
async function pressModifier(canvasElement: HTMLElement, name: string, holdMs: number) {
  const key = within(canvasElement).getByRole("button", { name });
  const init = { pointerId: 1, isPrimary: true, button: 0, bubbles: true };
  key.dispatchEvent(new PointerEvent("pointerdown", init));
  await new Promise((resolve) => {
    setTimeout(resolve, holdMs);
  });
  key.dispatchEvent(new PointerEvent("pointerup", init));
}

export const PhoneIdle: Story = {
  render: (args) => (
    <Surface width={390}>
      <TerminalExtraKeys {...args} />
    </Surface>
  ),
};

export const PhoneArmed: Story = {
  render: (args) => (
    <Surface width={390}>
      <TerminalExtraKeys {...args} />
    </Surface>
  ),
  play: async ({ canvasElement }) => {
    await pressModifier(canvasElement, "Control", 0);
  },
};

export const PhoneLocked: Story = {
  render: (args) => (
    <Surface width={390}>
      <TerminalExtraKeys {...args} />
    </Surface>
  ),
  play: async ({ canvasElement }) => {
    await pressModifier(canvasElement, "Control", LONG_PRESS_MS + 100);
  },
};

export const TabletIdle: Story = {
  render: (args) => (
    <Surface width={1024}>
      <TerminalExtraKeys {...args} />
    </Surface>
  ),
};

export const TabletLocked: Story = {
  render: (args) => (
    <Surface width={1024}>
      <TerminalExtraKeys {...args} />
    </Surface>
  ),
  play: async ({ canvasElement }) => {
    await pressModifier(canvasElement, "Shift", LONG_PRESS_MS + 100);
    await pressModifier(canvasElement, "Alt", 0);
  },
};
