// Link detection over the xterm buffer: which rows join into one URL and
// where the link lands. The buffer is driven headlessly with the same bytes
// the pane would receive.

import { type ILink, Terminal } from "@xterm/xterm";
import { describe, expect, it, vi } from "vitest";
import { TerminalLinkProvider } from "./TerminalLinkProvider";

const COLS = 40;
// Longer than the pane, so it needs a second row.
const URL = "https://wrapped.example.com/explore/connections/pagerduty-mcp";
// Needs three rows.
const LONG_URL = `${URL}?o=${"1234567890".repeat(3)}#end`;

/** Split ``text`` into rows of ``COLS`` characters joined by hard line breaks. */
function brokenAtPaneWidth(text: string): string {
  const rows: string[] = [];
  for (let i = 0; i < text.length; i += COLS) rows.push(text.slice(i, i + COLS));
  return `${rows.join("\r\n")}\r\n`;
}

async function terminalShowing(output: string): Promise<Terminal> {
  const term = new Terminal({ cols: COLS, rows: 10 });
  await new Promise<void>((resolve) => {
    term.write(output, resolve);
  });
  return term;
}

/** Links the provider reports for the 1-based buffer row. */
function linksOnRow(term: Terminal, row: number): ILink[] {
  let result: ILink[] | undefined;
  new TerminalLinkProvider(term, vi.fn()).provideLinks(row, (links) => {
    result = links;
  });
  return result ?? [];
}

function texts(links: ILink[]): string[] {
  return links.map((link) => link.text);
}

describe("TerminalLinkProvider", () => {
  it("joins the rows of a URL the terminal soft-wrapped", async () => {
    const term = await terminalShowing(`${URL}\r\n`);

    for (const row of [1, 2]) {
      const [link, ...rest] = linksOnRow(term, row);
      expect(rest).toEqual([]);
      expect(link.text).toBe(URL);
      expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 21, y: 2 } });
    }
  });

  it("joins the rows of a URL the program broke at the pane width", async () => {
    // A width-aware CLI ends the first row with its own line break after
    // exactly COLS characters, so xterm stores two unwrapped rows.
    const term = await terminalShowing(brokenAtPaneWidth(URL));
    expect(term.buffer.active.getLine(1)?.isWrapped).toBe(false);

    for (const row of [1, 2]) {
      const [link, ...rest] = linksOnRow(term, row);
      expect(rest).toEqual([]);
      expect(link.text).toBe(URL);
      expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 21, y: 2 } });
    }
  });

  it("follows a program-broken URL across more than two rows", async () => {
    const term = await terminalShowing(brokenAtPaneWidth(LONG_URL));

    for (const row of [1, 2, 3]) {
      const [link, ...rest] = linksOnRow(term, row);
      expect(rest).toEqual([]);
      expect(link.text).toBe(LONG_URL);
      expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 18, y: 3 } });
    }
  });

  it.each([
    [
      "Open https://example.com/aaaaaaaaaaaaaaa",
      "README.md shows more",
      "https://example.com/aaaaaaaaaaaaaaa",
    ],
    ["See the console at https://ab.example.io", "for details.", "https://ab.example.io"],
  ])(
    "keeps the hard break after a row holding more than one token: %s",
    async (above, below, url) => {
      // Word-wrapped prose whose line happens to fill the pane exactly: the URL
      // fitted on its row, so the next row starts a new word.
      expect(above).toHaveLength(COLS);
      const term = await terminalShowing(`${above}\r\n${below}\r\n`);

      expect(texts(linksOnRow(term, 1))).toEqual([url]);
      expect(linksOnRow(term, 2)).toEqual([]);
    },
  );

  it("leaves an indented row out of the URL above it", async () => {
    const term = await terminalShowing(`${URL.slice(0, COLS)}\r\n  ${URL.slice(COLS)}\r\n`);

    expect(texts(linksOnRow(term, 1))).toEqual([URL.slice(0, COLS)]);
    expect(linksOnRow(term, 2)).toEqual([]);
  });

  it("ends an exact-width URL at the pane edge", async () => {
    const exact = `https://ab.example.io/${"x".repeat(COLS - 22)}`;
    expect(exact).toHaveLength(COLS);
    const term = await terminalShowing(`${exact}\r\n\r\nnext paragraph\r\n`);

    const [link] = linksOnRow(term, 1);
    expect(link.text).toBe(exact);
    // xterm compares linearized positions, so the exclusive end of a link
    // filling the row lands on the next row's column 0.
    expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 0, y: 2 } });
  });

  it("joins a lone full-width token to the non-blank row below it", async () => {
    // Accepted ambiguity: the buffer cannot tell this from a longer URL that a
    // program broke at the pane width, so the next row's first word is joined.
    const exact = `https://ab.example.io/${"x".repeat(COLS - 22)}`;
    const term = await terminalShowing(`${exact}\r\nnext paragraph\r\n`);

    const [link] = linksOnRow(term, 1);
    expect(link.text).toBe(`${exact}next`);
    expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 4, y: 2 } });
  });

  it("maps a URL that wraps early in front of a wide character", async () => {
    // 21 narrow cells, then ten 2-cell characters: the tenth does not fit in
    // the last cell, so xterm leaves it empty and wraps the character.
    const url = "https://wide.example/日本語日本語日本語日";
    const term = await terminalShowing(`${url}\r\n`);
    expect(term.buffer.active.getLine(1)?.isWrapped).toBe(true);

    const [link] = linksOnRow(term, 1);
    expect(link.text).toBe(url);
    expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 2, y: 2 } });
  });

  it("stops joining at a wrapped row that was erased blank", async () => {
    // A TUI that erases rows it had wrapped leaves them flagged as wrapped but
    // empty. Blank rows end a URL, so the scan must stop there instead of
    // walking the rest of the buffer on every hover.
    const blankRows = 300;
    const term = new Terminal({ cols: COLS, rows: blankRows + 2 });
    const exact = `https://ab.example.io/${"x".repeat(COLS - 22)}`;
    let output = `${exact}${"y".repeat(COLS * blankRows)}`;
    for (let row = 2; row <= blankRows + 1; row++) output += `\x1b[${row};1H\x1b[${COLS}X`;
    await new Promise<void>((resolve) => {
      term.write(output, resolve);
    });
    expect(term.buffer.active.getLine(1)?.isWrapped).toBe(true);
    expect(term.buffer.active.getLine(1)?.translateToString(true)).toBe("");
    const getLine = vi.spyOn(term.buffer.active, "getLine");

    const [link] = linksOnRow(term, 1);

    expect(link.text).toBe(exact);
    expect(link.range).toEqual({ start: { x: 1, y: 1 }, end: { x: 0, y: 2 } });
    expect(getLine.mock.calls.length).toBeLessThan(20);
  });

  it("excludes trailing punctuation and enclosing quotes", async () => {
    const term = await terminalShowing(
      `Docs: https://ab.example.io/docs. Config in "https://ab.example.io/cfg".\r\n`,
    );

    expect(texts(linksOnRow(term, 1))).toEqual([
      "https://ab.example.io/docs",
      "https://ab.example.io/cfg",
    ]);
  });

  it("reports nothing for rows without a URL", async () => {
    const term = await terminalShowing("plain text, no links here\r\n");

    expect(linksOnRow(term, 1)).toEqual([]);
  });

  it("activates with the detected URL", async () => {
    const activate = vi.fn();
    const term = await terminalShowing(brokenAtPaneWidth(URL));
    let links: ILink[] | undefined;
    new TerminalLinkProvider(term, activate).provideLinks(1, (result) => {
      links = result;
    });
    const event = new MouseEvent("click");

    links?.[0].activate(event, links[0].text);

    expect(activate).toHaveBeenCalledWith(event, URL);
  });
});
