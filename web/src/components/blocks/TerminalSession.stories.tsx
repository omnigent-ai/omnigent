import type { Meta, StoryObj } from "@storybook/react-vite";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef } from "react";
import { loadWebglRenderer, rtlCharacterJoinRanges } from "./TerminalSession";

function RtlTerminalPreview({ joined, renderer }: { joined: boolean; renderer: "dom" | "webgl" }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const term = new Terminal({
      cols: 68,
      rows: 9,
      fontSize: 18,
      fontFamily: "monospace",
      allowProposedApi: true,
      theme: { background: "#101214", foreground: "#eeeeee" },
    });
    term.open(container.current!);
    if (joined) term.registerCharacterJoiner(rtlCharacterJoinRanges);
    const webgl = renderer === "webgl" ? loadWebglRenderer(term) : null;
    term.write(
      [
        "\x1b[?25l$ printf 'example output'",
        "שלום עולם, החיבור עובד.",
        "\x1b[32mשלום עולם\x1b[0m — GitHub — \x1b[1mבדיקת חיבור\x1b[0m",
        "مرحبا بالعالم",
        "می\u200cروم خانه",
        "שָׁלוֹם עוֹלָם",
        "שלום 123 עולם!",
        "git push origin feature/example",
      ].join("\r\n"),
    );
    return () => {
      webgl?.dispose();
      term.dispose();
    };
  }, [joined, renderer]);
  return <div ref={container} className="overflow-hidden rounded border bg-[#101214] p-3" />;
}

const meta = {
  title: "Components/Blocks/Terminal RTL",
  args: { renderer: "webgl" as "dom" | "webgl" },
  argTypes: { renderer: { control: "radio", options: ["webgl", "dom"] } },
  render: ({ renderer }) => (
    <div className="w-[760px] max-w-full space-y-4">
      <div>
        <h3 className="mb-2">Cell-by-cell rendering</h3>
        <RtlTerminalPreview joined={false} renderer={renderer} />
      </div>
      <div>
        <h3 className="mb-2">Joined RTL phrases</h3>
        <RtlTerminalPreview joined renderer={renderer} />
      </div>
    </div>
  ),
} satisfies Meta<{ renderer: "dom" | "webgl" }>;

export default meta;
type Story = StoryObj<typeof meta>;
export const RightToLeftOutput: Story = {};
