// Unit tests for suppressing xterm.js's replies to pane queries. Each query is
// checked both ways, so an empty reply list means the suppression worked.

import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";
import { suppressTerminalQueryReplies } from "./TerminalQueryReplies";

const QUERIES: Record<string, string> = {
  "primary device attributes": "\x1b[c",
  "secondary device attributes": "\x1b[>c",
  "device status": "\x1b[5n",
  "cursor position": "\x1b[6n",
  "extended cursor position": "\x1b[?6n",
  "ANSI mode report": "\x1b[4$p",
  "DEC mode report": "\x1b[?25$p",
  "status string": "\x1bP$qm\x1b\\",
};

async function write(term: Terminal, data: string): Promise<void> {
  await new Promise<void>((resolve) => {
    term.write(data, resolve);
  });
}

/** Feed ``output`` to a terminal and collect what it sends back as input. */
async function repliesTo(output: string, { suppress }: { suppress: boolean }) {
  const term = new Terminal({ allowProposedApi: true });
  const suppression = suppress ? suppressTerminalQueryReplies(term) : null;
  const replies: string[] = [];
  term.onData((data) => replies.push(data));
  try {
    await write(term, output);
    return { replies, firstLine: term.buffer.active.getLine(0)?.translateToString(true) };
  } finally {
    suppression?.dispose();
    term.dispose();
  }
}

describe("suppressTerminalQueryReplies", () => {
  it.each(Object.entries(QUERIES))("leaves the %s query to tmux", async (_name, query) => {
    expect((await repliesTo(query, { suppress: false })).replies).not.toEqual([]);
    expect((await repliesTo(query, { suppress: true })).replies).toEqual([]);
  });

  it.each([4, 10, 11, 12])(
    "consumes OSC %i colour queries but lets colour changes through",
    async (code) => {
      // Colour reports need an opened renderer, so check routing instead: a
      // handler registered first only runs when the suppression passes on.
      const term = new Terminal({ allowProposedApi: true });
      const reachedXterm: string[] = [];
      term.parser.registerOscHandler(code, (data) => {
        reachedXterm.push(data);
        return true;
      });
      const suppression = suppressTerminalQueryReplies(term);
      const query = code === 4 ? "1;?" : "?";
      const change = code === 4 ? "1;#123456" : "#123456";

      await write(term, `\x1b]${code};${query}\x07\x1b]${code};${change}\x07`);

      expect(reachedXterm).toEqual([change]);
      suppression.dispose();
      term.dispose();
    },
  );

  it("keeps rendering the output around a query", async () => {
    const { replies, firstLine } = await repliesTo("before\x1b[6nafter", { suppress: true });

    expect(replies).toEqual([]);
    expect(firstLine).toBe("beforeafter");
  });

  it("answers queries again once disposed", async () => {
    const term = new Terminal({ allowProposedApi: true });
    const replies: string[] = [];
    term.onData((data) => replies.push(data));
    suppressTerminalQueryReplies(term).dispose();

    await write(term, "\x1b[6n");

    expect(replies).toEqual(["\x1b[1;1R"]);
    term.dispose();
  });
});
