// Unit tests for dropping xterm.js's replies to the pane queries tmux answers.
// Each query is checked both ways, so an empty reply list means it was dropped
// and a non-empty one means the program still gets xterm.js's answer.

import { Terminal } from "@xterm/xterm";
import { describe, expect, it } from "vitest";
import { parseTmuxRepliesMessage, suppressRepliesTmuxSends } from "./TerminalQueryReplies";

// Each query xterm.js answers, with the identifier the runner reports it by.
const QUERIES: [name: string, query: string, id: string][] = [
  ["primary device attributes", "\x1b[c", "da1"],
  ["secondary device attributes", "\x1b[>c", "da2"],
  ["device status", "\x1b[5n", "dsr:5"],
  ["cursor position", "\x1b[6n", "dsr:6"],
  ["extended cursor position", "\x1b[?6n", "dsr:?6"],
  ["ANSI mode report", "\x1b[4$p", "decrqm:4"],
  ["DEC mode report", "\x1b[?2004$p", "decrqm:?2004"],
  ["status string", "\x1bP$qm\x1b\\", "decrqss:m"],
];

async function write(term: Terminal, data: string): Promise<void> {
  await new Promise<void>((resolve) => {
    term.write(data, resolve);
  });
}

/** Feed ``output`` to a terminal and collect what it sends back as input. */
async function repliesTo(output: string, tmuxAnswers: Iterable<string>) {
  const term = new Terminal({ allowProposedApi: true });
  const suppression = suppressRepliesTmuxSends(term, new Set(tmuxAnswers));
  const replies: string[] = [];
  term.onData((data) => replies.push(data));
  try {
    await write(term, output);
    return { replies, firstLine: term.buffer.active.getLine(0)?.translateToString(true) };
  } finally {
    suppression.dispose();
    term.dispose();
  }
}

/** The OSC payloads that reach xterm.js's own handler past the suppression. */
async function oscReachingXterm(code: number, payloads: string[], tmuxAnswers: string[]) {
  // Colour reports need an opened renderer, so check routing instead: a
  // handler registered first only runs when the suppression passes on.
  const term = new Terminal({ allowProposedApi: true });
  const reachedXterm: string[] = [];
  term.parser.registerOscHandler(code, (data) => {
    reachedXterm.push(data);
    return true;
  });
  const suppression = suppressRepliesTmuxSends(term, new Set(tmuxAnswers));
  try {
    await write(term, payloads.map((payload) => `\x1b]${code};${payload}\x07`).join(""));
    return reachedXterm;
  } finally {
    suppression.dispose();
    term.dispose();
  }
}

describe("suppressRepliesTmuxSends", () => {
  it.each(QUERIES)("drops the %s reply only when tmux answers it", async (_name, query, id) => {
    expect((await repliesTo(query, [])).replies).not.toEqual([]);
    expect((await repliesTo(query, [id])).replies).toEqual([]);
  });

  it("answers what tmux 3.6 leaves unanswered", async () => {
    // tmux 3.6 reports a few named DEC modes and nothing on private status.
    const tmux36 = ["da1", "da2", "dsr:5", "dsr:6", "decrqm:?2004", "decrqss:*"];

    expect((await repliesTo("\x1b[?2004$p", tmux36)).replies).toEqual([]);
    expect((await repliesTo("\x1b[?25$p", tmux36)).replies).toEqual(["\x1b[?25;1$y"]);
    expect((await repliesTo("\x1b[?6n", tmux36)).replies).toEqual(["\x1b[?1;1R"]);
  });

  it.each([
    ["DEC mode", "decrqm:?*", "\x1b[?9999$p"],
    ["ANSI mode", "decrqm:*", "\x1b[9999$p"],
    ["status string", "decrqss:*", "\x1bP$qzz\x1b\\"],
  ])("treats a wildcard as every %s request", async (_kind, wildcard, query) => {
    expect((await repliesTo(query, [])).replies).not.toEqual([]);
    expect((await repliesTo(query, [wildcard])).replies).toEqual([]);
  });

  it("ignores device attribute requests xterm.js wouldn't answer", async () => {
    // A non-zero parameter gets no reply from xterm.js, so nothing to drop.
    expect((await repliesTo("\x1b[1c", ["da1"])).replies).toEqual([]);
  });

  it.each([4, 10, 11, 12])("consumes an OSC %i query only when tmux answers it", async (code) => {
    const query = code === 4 ? "1;?" : "?";
    const change = code === 4 ? "1;#123456" : "#123456";

    expect(await oscReachingXterm(code, [query, change], [`osc:${code}`])).toEqual([change]);
    expect(await oscReachingXterm(code, [query, change], [])).toEqual([query, change]);
  });

  it("passes a colour query mixed with a change to xterm.js", async () => {
    expect(await oscReachingXterm(4, ["1;?;2;#123456"], ["osc:4"])).toEqual(["1;?;2;#123456"]);
  });

  it("passes a combined colour query to xterm.js, as tmux leaves it unanswered", async () => {
    expect(await oscReachingXterm(10, ["?;?"], ["osc:10", "osc:11"])).toEqual(["?;?"]);
    expect(await oscReachingXterm(4, ["1;?;2;?"], ["osc:4"])).toEqual(["1;?;2;?"]);
  });

  it("keeps rendering the output around a dropped query", async () => {
    const { replies, firstLine } = await repliesTo("before\x1b[6nafter", ["dsr:6"]);

    expect(replies).toEqual([]);
    expect(firstLine).toBe("beforeafter");
  });

  it("answers queries again once disposed", async () => {
    const term = new Terminal({ allowProposedApi: true });
    const replies: string[] = [];
    term.onData((data) => replies.push(data));
    suppressRepliesTmuxSends(term, new Set(["dsr:6"])).dispose();

    await write(term, "\x1b[6n");

    expect(replies).toEqual(["\x1b[1;1R"]);
    term.dispose();
  });
});

describe("parseTmuxRepliesMessage", () => {
  it("reads the queries tmux answers", () => {
    const message = JSON.stringify({ type: "tmux-replies", queries: ["da1", "dsr:6"] });

    expect(parseTmuxRepliesMessage(message)).toEqual(new Set(["da1", "dsr:6"]));
  });

  it.each([
    [
      "a clipboard message",
      JSON.stringify({ type: "clipboard-write", encoding: "base64", data: "" }),
    ],
    ["a non-string query", JSON.stringify({ type: "tmux-replies", queries: ["da1", 6] })],
    ["a missing query list", JSON.stringify({ type: "tmux-replies" })],
    ["malformed JSON", "{"],
  ])("rejects %s", (_name, message) => {
    expect(parseTmuxRepliesMessage(message)).toBeNull();
  });
});
