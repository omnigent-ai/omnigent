import type { Meta, StoryObj } from "@storybook/react-vite";
import { Terminal } from "@xterm/xterm";
import { useEffect, useRef, useState } from "react";
import { TerminalLinkProvider } from "./TerminalLinkProvider";
import { TerminalBidi } from "./TerminalBidi";
import { loadWebglRenderer, rtlCharacterJoinRanges } from "./TerminalSession";

function RtlTerminalPreview({ joined, renderer }: { joined: boolean; renderer: "dom" | "webgl" }) {
  const container = useRef<HTMLDivElement>(null);
  const [selection, setSelection] = useState("");
  const [activatedLink, setActivatedLink] = useState("");
  useEffect(() => {
    const term = new Terminal({
      cols: 60,
      rows: 9,
      fontSize: 18,
      fontFamily: "monospace",
      allowProposedApi: true,
      theme: { background: "#101214", foreground: "#eeeeee" },
    });
    term.registerLinkProvider(
      new TerminalLinkProvider(term, (_event, uri) => setActivatedLink(uri)),
    );
    term.open(container.current!);
    if (joined) term.registerCharacterJoiner(rtlCharacterJoinRanges);
    let webgl = renderer === "webgl" ? loadWebglRenderer(term) : null;
    const bidi = joined
      ? new TerminalBidi(term, () => {
          webgl?.dispose();
          webgl = null;
        })
      : null;
    const selectionChanged = term.onSelectionChange(() => setSelection(term.getSelection()));
    term.write(
      [
        "\x1b[?25l$ printf 'example output'",
        "שלום עולם, החיבור עובד.",
        "\x1b[32mשלום עולם\x1b[0m — GitHub — \x1b[1mבדיקת חיבור\x1b[0m",
        "مرحبا بالعالم",
        "می\u200cروم خانه",
        "שָׁלוֹם עוֹלָם",
        "שלום 123 עולם!",
        "שלום https://example.com עולם",
        "git push origin feature/example",
      ].join("\r\n"),
    );
    return () => {
      selectionChanged.dispose();
      bidi?.dispose();
      webgl?.dispose();
      term.dispose();
    };
  }, [joined, renderer]);
  return (
    <div>
      <div ref={container} className="overflow-hidden rounded border bg-[#101214] p-3" />
      <output aria-label="Activated terminal link" className="block min-h-6">
        {activatedLink}
      </output>
      <output aria-label="Selected terminal text" className="block min-h-6 font-mono" dir="auto">
        {selection}
      </output>
    </div>
  );
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
        <h3 className="mb-2">Automatic paragraph direction</h3>
        <RtlTerminalPreview joined renderer={renderer} />
      </div>
    </div>
  ),
} satisfies Meta<{ renderer: "dom" | "webgl" }>;

export default meta;
type Story = StoryObj<typeof meta>;
export const RightToLeftOutput: Story = {};
