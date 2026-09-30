import { describe, expect, it } from "vitest";
import {
  GenerationalCache,
  MAX_PARAGRAPH_LINES,
  MD_BOLD,
  MD_CODE,
  MD_FENCE,
  MD_INFO,
  MD_ITALIC,
  MD_LINK,
  MD_LINK_URL,
  MD_MARKER,
  MD_OPEN_FENCE,
  MD_QUOTE,
  MD_STRIKE,
  markerRevealRanges,
  normalizeLineBreaks,
  plainComposerMarkdown,
  tokenizeComposerMarkdown,
} from "./composerMarkdown";

const FLAG_NAMES: [number, string][] = [
  [MD_ITALIC, "italic"],
  [MD_BOLD, "bold"],
  [MD_CODE, "code"],
  [MD_FENCE, "fence"],
  [MD_INFO, "info"],
  [MD_OPEN_FENCE, "open"],
  [MD_STRIKE, "strike"],
  [MD_LINK, "link"],
  [MD_LINK_URL, "url"],
  [MD_QUOTE, "quote"],
  [MD_MARKER, "marker"],
];

/** Every styled run as `[text, "flag+flag"]`; unstyled runs are omitted. */
function styledRuns(text: string): [string, string][] {
  const { blocks } = tokenizeComposerMarkdown(text);
  const runs: [string, string][] = [];
  for (const block of blocks) {
    for (const segment of block.segments) {
      if (segment.flags === 0) continue;
      const names = FLAG_NAMES.filter(([flag]) => segment.flags & flag).map(([, name]) => name);
      runs.push([block.text.slice(segment.start, segment.end), names.join("+")]);
    }
  }
  return runs;
}

/** Blocks and their segments must tile the draft exactly, in order. */
function reassemble(text: string): string {
  const { blocks } = tokenizeComposerMarkdown(text);
  let offset = 0;
  let out = "";
  for (const block of blocks) {
    expect(block.start).toBe(offset);
    let segmentOffset = 0;
    for (const segment of block.segments) {
      expect(segment.start).toBe(segmentOffset);
      expect(segment.end).toBeGreaterThan(segment.start);
      segmentOffset = segment.end;
      out += block.text.slice(segment.start, segment.end);
    }
    expect(segmentOffset).toBe(block.text.length);
    offset += block.text.length;
  }
  return out;
}

describe("tokenizeComposerMarkdown", () => {
  it("styles emphasis, strong and inline code with visible markers", () => {
    expect(styledRuns("_this_ and *that*")).toEqual([
      ["_", "marker"],
      ["this", "italic"],
      ["_", "marker"],
      ["*", "marker"],
      ["that", "italic"],
      ["*", "marker"],
    ]);
    expect(styledRuns("**this** __too__")).toEqual([
      ["**", "marker"],
      ["this", "bold"],
      ["**", "marker"],
      ["__", "marker"],
      ["too", "bold"],
      ["__", "marker"],
    ]);
    expect(styledRuns("run `pnpm test` now")).toEqual([
      ["`", "code+marker"],
      ["pnpm test", "code"],
      ["`", "code+marker"],
    ]);
  });

  it("nests strong and emphasis", () => {
    expect(styledRuns("***both***")).toEqual([
      ["*", "marker"],
      ["**", "italic+marker"],
      ["both", "italic+bold"],
      ["**", "italic+marker"],
      ["*", "marker"],
    ]);
    expect(styledRuns("**bold _and italic_**")).toEqual([
      ["**", "marker"],
      ["bold ", "bold"],
      ["_", "bold+marker"],
      ["and italic", "italic+bold"],
      ["_", "bold+marker"],
      ["**", "marker"],
    ]);
  });

  it("leaves intraword underscores plain (snake_case, file names)", () => {
    for (const text of [
      "snake_case_identifier",
      "open file_name.py and my_var_name",
      "my__dunder__attr",
      "a_b_ c",
      "CONSTANT_VALUE_HERE",
    ]) {
      expect(styledRuns(text), text).toEqual([]);
    }
    // Matches the transcript's CommonMark renderer, which bolds a leading dunder.
    expect(styledRuns("__init__.py")).toEqual([
      ["__", "marker"],
      ["init", "bold"],
      ["__", "marker"],
    ]);
    expect(styledRuns("call _snake_case_ now")).toEqual([
      ["_", "marker"],
      ["snake_case", "italic"],
      ["_", "marker"],
    ]);
  });

  it("lets * emphasize intraword, per CommonMark", () => {
    expect(styledRuns("un*frigging*believable")).toEqual([
      ["*", "marker"],
      ["frigging", "italic"],
      ["*", "marker"],
    ]);
  });

  it("does not style escaped markers", () => {
    expect(styledRuns("\\_not italic\\_")).toEqual([]);
    expect(styledRuns("\\*not italic\\*")).toEqual([]);
    expect(styledRuns("\\*\\*not bold\\*\\*")).toEqual([]);
    expect(styledRuns("\\`not code\\`")).toEqual([]);
    // Only the escaped opener is literal; the rest still pairs.
    expect(styledRuns("\\**a*")).toEqual([
      ["*", "marker"],
      ["a", "italic"],
      ["*", "marker"],
    ]);
  });

  it("leaves unclosed inline markers unstyled", () => {
    for (const text of [
      "_unclosed",
      "**unclosed bold",
      "`unclosed code",
      "a * b * c",
      "2 * 3 = 6",
      "* list item",
      "**",
      "`` a ` b",
    ]) {
      expect(styledRuns(text), text).toEqual([]);
    }
    // A mismatched closer does not steal a partner of the other kind.
    expect(styledRuns("*one_")).toEqual([]);
  });

  it("does not parse inside code spans", () => {
    expect(styledRuns("`**not bold** _x_`")).toEqual([
      ["`", "code+marker"],
      ["**not bold** _x_", "code"],
      ["`", "code+marker"],
    ]);
    // Backslash escapes don't apply inside code; the span closes at the backtick.
    expect(styledRuns("`a\\`")).toEqual([
      ["`", "code+marker"],
      ["a\\", "code"],
      ["`", "code+marker"],
    ]);
    expect(styledRuns("``has ` tick``")).toEqual([
      ["``", "code+marker"],
      ["has ` tick", "code"],
      ["``", "code+marker"],
    ]);
    // Code spans take precedence over emphasis that would cross them.
    expect(styledRuns("*a `b*` c")).toEqual([
      ["`", "code+marker"],
      ["b*", "code"],
      ["`", "code+marker"],
    ]);
  });

  it("styles a fenced block, only its backticks as markers, without parsing its body", () => {
    const text = "before\n```ts\nconst a_b = **x**;\n```\nafter _it_";
    expect(styledRuns(text)).toEqual([
      ["```", "fence+marker"],
      ["ts", "fence+info"],
      ["\nconst a_b = **x**;\n", "fence"],
      ["```", "fence+marker"],
      ["\n", "fence"],
      ["_", "marker"],
      ["it", "italic"],
      ["_", "marker"],
    ]);
    expect(styledRuns("~~~\n*x*\n~~~")).toEqual([
      ["~~~", "fence+marker"],
      ["\n*x*\n", "fence"],
      ["~~~", "fence+marker"],
    ]);
    // Indents and trailing spaces stay plain fence text; only the runs are markers.
    expect(styledRuns("  ```  ts  \ncode\n  ```  \n")).toEqual([
      ["  ", "fence"],
      ["```", "fence+marker"],
      ["  ts  ", "fence+info"],
      ["\ncode\n  ", "fence"],
      ["```", "fence+marker"],
      ["  \n", "fence"],
    ]);
  });

  it("needs a closing fence of the same character and at least the opener's length", () => {
    expect(styledRuns("````\n```\nstill code\n````")).toEqual([
      ["````", "fence+marker"],
      ["\n```\nstill code\n", "fence"],
      ["````", "fence+marker"],
    ]);
    expect(styledRuns("```\n~~~\n```")).toEqual([
      ["```", "fence+marker"],
      ["\n~~~\n", "fence"],
      ["```", "fence+marker"],
    ]);
  });

  it("styles an unclosed fence through to the end, its backticks always shown", () => {
    const text = "intro\n```py\ndef f(): pass\n_x_";
    expect(styledRuns(text)).toEqual([
      ["```", "fence+open"],
      ["py", "fence+info"],
      ["\ndef f(): pass\n_x_", "fence"],
    ]);
    expect(styledRuns("```")).toEqual([["```", "fence+open"]]);
    // No token: an unclosed fence has nothing to reveal or hide.
    expect(tokenizeComposerMarkdown(text).blocks.flatMap((block) => block.tokens)).toEqual([]);
  });

  it("does not treat a backtick info string or deep indent as a fence", () => {
    expect(styledRuns("``` a`b")).toEqual([]);
    expect(styledRuns("    ```\ncode")).toEqual([]);
  });

  it("keeps emphasis within a paragraph", () => {
    expect(styledRuns("*spans\nlines*")).toEqual([
      ["*", "marker"],
      ["spans\nlines", "italic"],
      ["*", "marker"],
    ]);
    expect(styledRuns("*no\n\nblank line crossing*")).toEqual([]);
  });

  it("tiles the draft exactly, so the layer shows the draft byte for byte", () => {
    const samples = [
      "",
      "plain",
      "trailing newline\n",
      "\n\n\n",
      "mixed _a_ **b** `c`\n\n```\nfence\n```\n\ntail *d*",
      "emoji 🎉 _ünïcödé_ and CJK 日本語 **強調**",
      "tabs\tand  spaces _x_\t\n",
      "```\nunclosed",
    ];
    for (const text of samples) expect(reassemble(text), JSON.stringify(text)).toBe(text);
  });

  it("reports whether anything is styled", () => {
    expect(tokenizeComposerMarkdown("plain prose, snake_case").styled).toBe(false);
    expect(tokenizeComposerMarkdown("").styled).toBe(false);
    expect(tokenizeComposerMarkdown("some _it_").styled).toBe(true);
    expect(tokenizeComposerMarkdown("```").styled).toBe(true);
  });

  it("reuses an unchanged paragraph's segments across drafts", () => {
    const first = tokenizeComposerMarkdown("para _one_\n\npara two");
    const second = tokenizeComposerMarkdown("para _one_\n\npara two edited");
    expect(second.blocks[0].segments).toBe(first.blocks[0].segments);
  });

  it("styles a very long paragraph line by line", () => {
    const lines = Array.from({ length: MAX_PARAGRAPH_LINES + 1 }, (_, i) => `row ${i} **b**`);
    const text = ["*spans", "lines*", ...lines].join("\n");
    const { blocks } = tokenizeComposerMarkdown(text);
    expect(blocks).toHaveLength(lines.length + 2);
    expect(reassemble(text)).toBe(text);
    // Line-local styling still applies; only the cross-line emphasis is lost.
    expect(styledRuns(text).filter(([, flags]) => flags === "bold")).toHaveLength(lines.length);
    expect(styledRuns(text).filter(([, flags]) => flags === "italic")).toEqual([]);
    const short = tokenizeComposerMarkdown(["*spans", "lines*"].join("\n"));
    expect(short.blocks).toHaveLength(1);
  });

  it("keeps recently used cache entries across a rotation", () => {
    const cache = new GenerationalCache<number>(2);
    cache.set("a", 1);
    cache.set("b", 2);
    cache.set("c", 3); // rotates: a and b age into the previous generation
    expect(cache.get("a")).toBe(1); // promoted back into the current one
    cache.set("d", 4); // rotates again: b (unused) ages out after this one
    cache.set("e", 5);
    expect(cache.get("b")).toBeUndefined();
    expect(cache.get("a")).toBe(1);
  });

  it("never styles inside code resolved across lines of a long paragraph", () => {
    const filler = Array.from({ length: MAX_PARAGRAPH_LINES + 1 }, () => "filler");
    const text = ["`multi **not bold**", "line` **yes**", ...filler].join("\n");
    expect(tokenizeComposerMarkdown(text).blocks.length).toBeGreaterThan(MAX_PARAGRAPH_LINES);
    expect(reassemble(text)).toBe(text);
    const runs = styledRuns(text);
    expect(runs.filter(([, flags]) => flags.includes("bold"))).toEqual([["yes", "bold"]]);
    expect(runs.filter(([, flags]) => flags === "code").map(([t]) => t)).toEqual([
      "multi **not bold**\n",
      "line",
    ]);
    // Same code without the long-paragraph fallback agrees.
    const short = styledRuns(["`multi **not bold**", "line` **yes**"].join("\n"));
    expect(short.filter(([, flags]) => flags.includes("bold"))).toEqual([["yes", "bold"]]);
  });

  it("treats CRLF and a bare CR as line breaks, keeping source offsets", () => {
    const fenced = "```\r\ncode\r\n```\r\n**after**";
    expect(styledRuns(fenced)).toEqual([
      ["```", "fence+marker"],
      ["\r\ncode\r\n", "fence"],
      ["```", "fence+marker"],
      ["\r\n", "fence"],
      ["**", "marker"],
      ["after", "bold"],
      ["**", "marker"],
    ]);
    expect(styledRuns("*start\r\n\r\nend*")).toEqual([]);
    expect(styledRuns("*start\r\rend*")).toEqual([]);
    expect(styledRuns("```\rcode\r```\r_it_")).toEqual([
      ["```", "fence+marker"],
      ["\rcode\r", "fence"],
      ["```", "fence+marker"],
      ["\r", "fence"],
      ["_", "marker"],
      ["it", "italic"],
      ["_", "marker"],
    ]);
    // Emphasis still spans a single CR line break inside a paragraph.
    expect(styledRuns("*a\rb*")).toEqual([
      ["*", "marker"],
      ["a\rb", "italic"],
      ["*", "marker"],
    ]);
    for (const text of [fenced, "a\r\n\r\nb _c_\r\n", "x\ry\r\n\rz"])
      expect(reassemble(text)).toBe(text);
    const { blocks } = tokenizeComposerMarkdown("para one\r\n\r\npara two");
    expect(blocks.map((block) => [block.start, block.text])).toEqual([
      [0, "para one\r\n\r\n"],
      [12, "para two"],
    ]);
  });

  it("follows CommonMark flanking with no adjacent-marker exception", () => {
    // micromark 4 styles `_b` here (it lets a neighboring * or _ open a run);
    // CommonMark's flanking rules do not.
    expect(styledRuns("a*_b*c")).toEqual([]);
    expect(styledRuns("**_bold italic_**")).toEqual([
      ["**", "marker"],
      ["_", "bold+marker"],
      ["bold italic", "italic+bold"],
      ["_", "bold+marker"],
      ["**", "marker"],
    ]);
  });

  it("classifies neighbors per UTF-16 unit, as micromark does", () => {
    // An emoji's surrogate half counts as a letter, so this `_` is intraword.
    expect(styledRuns("🙂_x_🙂")).toEqual([]);
    // A BMP symbol is punctuation, so the same shape emphasizes.
    expect(styledRuns("©_x_©")).toEqual([
      ["_", "marker"],
      ["x", "italic"],
      ["_", "marker"],
    ]);
  });

  it("normalizes line breaks as a textarea does and maps offsets", () => {
    const { text, offset } = normalizeLineBreaks("a\r\nb\rc\r\n");
    expect(text).toBe("a\nb\nc\n");
    expect([0, 1, 2, 3, 4, 5, 6, 7, 8].map(offset)).toEqual([0, 1, 1, 2, 3, 4, 5, 5, 6]);
    expect(normalizeLineBreaks("plain\n").offset(4)).toBe(4);
  });

  it("builds unstyled per-line blocks for an over-long draft", () => {
    const text = "**a**\r\n_b_\nc";
    const plain = plainComposerMarkdown(text);
    expect(plain.styled).toBe(false);
    expect(plain.blocks.map((block) => block.text)).toEqual(["**a**\r\n", "_b_\n", "c"]);
    expect(plain.blocks.every((block) => block.segments.every((s) => s.flags === 0))).toBe(true);
  });

  it("stays fast on a large paste", () => {
    const lines: string[] = [];
    for (let i = 0; i < 5000; i++) {
      if (i % 50 === 0) lines.push("```");
      lines.push(`line ${i} with _em_, **strong**, \`code\`, snake_case_${i} and a * b`);
      if (i % 7 === 0) lines.push("");
    }
    const text = lines.join("\n");
    const started = performance.now();
    const result = tokenizeComposerMarkdown(text);
    expect(result.styled).toBe(true);
    expect(performance.now() - started).toBeLessThan(1000);
    // Keystrokes in the middle re-parse only the paragraph they land in.
    const middle = Math.floor(text.length / 2);
    const typed = performance.now();
    for (let i = 0; i < 20; i++)
      tokenizeComposerMarkdown(`${text.slice(0, middle)}${"x".repeat(i)}${text.slice(middle)}`);
    expect((performance.now() - typed) / 20).toBeLessThan(100);
  });

  it("stays fast typing into one huge paragraph", () => {
    const text = Array.from({ length: 5000 }, (_, i) => `line ${i} with _em_ and **b**`).join("\n");
    tokenizeComposerMarkdown(text);
    const middle = Math.floor(text.length / 2);
    const typed = performance.now();
    for (let i = 0; i < 20; i++)
      tokenizeComposerMarkdown(`${text.slice(0, middle)}${"x".repeat(i)}${text.slice(middle)}`);
    expect((performance.now() - typed) / 20).toBeLessThan(100);
  });

  it("stays linear on pathological delimiter and backtick runs", () => {
    const text = `${"*a ".repeat(20000)} ${"_ ".repeat(20000)} ${"` ``".repeat(10000)}`;
    const started = performance.now();
    tokenizeComposerMarkdown(text);
    expect(performance.now() - started).toBeLessThan(1000);
  });
});

describe("strikethrough, links and block quotes", () => {
  it("strikes through one or two tildes, as GFM does", () => {
    expect(styledRuns("a ~~gone~~ b")).toEqual([
      ["~~", "marker"],
      ["gone", "strike"],
      ["~~", "marker"],
    ]);
    expect(styledRuns("a ~gone~ b")).toEqual([
      ["~", "marker"],
      ["gone", "strike"],
      ["~", "marker"],
    ]);
    // Runs must match in length; three or more tildes are literal.
    expect(styledRuns("~~a~ b~~~c~~~")).toEqual([]);
    expect(styledRuns("~~ a~~")).toEqual([]);
    expect(styledRuns("\\~~a~~")).toEqual([]);
    expect(styledRuns("`~~code~~`").map(([t, f]) => [t, f])).toEqual([
      ["`", "code+marker"],
      ["~~code~~", "code"],
      ["`", "code+marker"],
    ]);
  });

  it("nests strikethrough with emphasis", () => {
    expect(styledRuns("~~a **b** c~~")).toEqual([
      ["~~", "marker"],
      ["a ", "strike"],
      ["**", "strike+marker"],
      ["b", "bold+strike"],
      ["**", "strike+marker"],
      [" c", "strike"],
      ["~~", "marker"],
    ]);
  });

  it("styles a link's text, hides its brackets and keeps its url visible", () => {
    expect(styledRuns("see [the docs](https://example.com/a) now")).toEqual([
      ["[", "marker"],
      ["the docs", "link"],
      ["](", "marker"],
      ["https://example.com/a", "url"],
      [")", "marker"],
    ]);
    expect(styledRuns('[t](<a b> "title")')).toEqual([
      ["[", "marker"],
      ["t", "link"],
      ["](", "marker"],
      ['<a b> "title"', "url"],
      [")", "marker"],
    ]);
    expect(styledRuns("[a](x_(y)_z)")).toEqual([
      ["[", "marker"],
      ["a", "link"],
      ["](", "marker"],
      ["x_(y)_z", "url"],
      [")", "marker"],
    ]);
  });

  it("parses emphasis inside link text, but never across its edge", () => {
    expect(styledRuns("[**b**](u)")).toEqual([
      ["[", "marker"],
      ["**", "link+marker"],
      ["b", "bold+link"],
      ["**", "link+marker"],
      ["](", "marker"],
      ["u", "url"],
      [")", "marker"],
    ]);
    expect(styledRuns("*a [b*](u)").filter(([, f]) => f.includes("italic"))).toEqual([]);
  });

  it("leaves images, escapes, code, bad destinations and reference links unstyled", () => {
    expect(styledRuns("![alt](pic.png)")).toEqual([]);
    expect(styledRuns("\\[a](b)")).toEqual([]);
    expect(styledRuns("[a]\\(b)")).toEqual([]);
    expect(styledRuns("[a](b c)")).toEqual([]);
    expect(styledRuns("[a](b")).toEqual([]);
    expect(styledRuns("[a][ref]")).toEqual([]);
    expect(styledRuns("`[a](b)`").map(([, f]) => f)).toEqual([
      "code+marker",
      "code",
      "code+marker",
    ]);
  });

  it("uses the innermost of nested links", () => {
    expect(styledRuns("[a [b](c) d](e)").filter(([, f]) => f === "link")).toEqual([["b", "link"]]);
  });

  it("marks a quote's `>` and dims its lines, lazy continuation included", () => {
    expect(styledRuns("> quoted\nlazy\n\nplain")).toEqual([
      [">", "quote+marker"],
      [" quoted\nlazy\n", "quote"],
    ]);
    expect(styledRuns("> a\n>\nb")).toEqual([
      [">", "quote+marker"],
      [" a\n", "quote"],
      [">", "quote+marker"],
      ["\n", "quote"],
    ]);
    expect(styledRuns("para\n> q")).toEqual([
      [">", "quote+marker"],
      [" q", "quote"],
    ]);
    expect(styledRuns("> > deep")).toEqual([
      [">", "quote+marker"],
      [" ", "quote"],
      [">", "quote+marker"],
      [" deep", "quote"],
    ]);
  });

  it("ends a quote's lazy continuation at a heading, list item or rule", () => {
    for (const next of ["# h", "- item", "1. item", "***"]) {
      const quoted = styledRuns(`> a\n${next}`).filter(([, f]) => f.includes("quote"));
      expect(quoted.map(([t]) => t).join(""), next).toBe("> a\n");
    }
  });

  it("parses inline styles inside a quote, across its lines", () => {
    expect(styledRuns("> *em\n> more*")).toEqual([
      [">", "quote+marker"],
      [" ", "quote"],
      ["*", "quote+marker"],
      ["em\n", "italic+quote"],
      [">", "italic+quote+marker"],
      [" more", "italic+quote"],
      ["*", "quote+marker"],
    ]);
    // Not a quote inside code or a fence, or behind four spaces.
    expect(styledRuns("```\n> not\n```").some(([, f]) => f.includes("quote"))).toBe(false);
    expect(styledRuns("    > code")).toEqual([]);
  });

  it("parses a fenced block inside a quote as code, keeping the quote's markers", () => {
    const text = "> ```ts\n> const **a** = 1\n> ```\nafter";
    expect(styledRuns(text)).toEqual([
      [">", "quote+marker"],
      [" ", "fence"],
      ["```", "fence+marker"],
      ["ts", "fence+info"],
      ["\n", "fence"],
      [">", "quote+marker"],
      [" const **a** = 1\n", "fence"],
      [">", "quote+marker"],
      [" ", "fence"],
      ["```", "fence+marker"],
      ["\n", "fence"],
    ]);
    const [block] = tokenizeComposerMarkdown(text).blocks;
    expect(block.fence).toEqual({
      open: false,
      run: "```",
      depth: 1,
      closeLine: 26,
      prefix: "> ",
      lines: [2, 10, 28],
    });
    // An unclosed quoted block ends with its quote; a deeper `>` is code text.
    const open = tokenizeComposerMarkdown("> ```\n> > code\nplain").blocks;
    expect(open.map((b) => [b.text, b.fence?.open])).toEqual([
      ["> ```\n> > code\n", true],
      ["plain", undefined],
    ]);
    expect(reassemble(text)).toBe(text);
  });

  it("continues only an open quoted paragraph lazily", () => {
    for (const quoted of [
      "> # heading",
      "> ***",
      "> title\n> ===",
      "> a\n> b\n> ---",
      ">     code",
      "> para\n>\n>     code",
      ">\t\tcode",
      ">\t  code",
      "> para\nlazy\n> ===",
      // A tab after `>` gives it one column: `===` is indented 2, an underline.
      "> para\n>\t===",
      "> para\n>  \t===",
      // A list item's heading, rule or emptiness isn't a paragraph either.
      "> - # heading",
      "> 1. # heading",
      "> -",
      "> - ---",
      "> - item\n> *",
      "> 2. item\n>\t\t===",
      // A lazy line starting an item, even an empty one, starts a new block.
      "> > para\n> -",
    ]) {
      const runs = styledRuns(`${quoted}\noutside`);
      expect(runs.map(([t]) => t).join(""), quoted).not.toContain("outside");
    }
    // A paragraph goes on through indented or `===` lines, and an `===` with no
    // paragraph above it is one.
    for (const quoted of [
      "> para",
      "> para\n>     more",
      "> ===",
      ">\tcode",
      ">  \tcode",
      // Indented 4 past a tab's column, `===` is the paragraph's text.
      "> para\n>\t\t===",
      // A list item's paragraph goes on, through lines of its own or lazily.
      "> - item",
      "> - item\n> ===",
      "> - # h\n>     code",
      "> 2. item\n> 3. more",
      // A shallower `>` line goes on a deeper quote's paragraph.
      "> > para\n>     code",
    ]) {
      const runs = styledRuns(`${quoted}\nlazy`);
      expect(runs.map(([t]) => t).join(""), quoted).toMatch(/lazy$/);
    }
  });

  it("keeps both tab-after-`>` underline cases exact", () => {
    // One column goes to the marker: `===` is indented 2, an underline.
    const heading = styledRuns("> para\n>\t===\nlazy");
    expect(heading.map(([t]) => t).join("")).not.toContain("lazy");
    // Two more spaces make it 4: paragraph text, and `lazy` goes on the quote.
    const lazy = styledRuns("> para\n>\t  ===\nlazy");
    expect(lazy.map(([t]) => t).join("")).toMatch(/lazy$/);
  });

  it("parses a fence inside a quoted list item as code, keeping its containers", () => {
    const text = "> - ```\n>   ** not bold**\n>   ```\nafter **b**";
    const [block, rest] = tokenizeComposerMarkdown(text).blocks;
    expect(block.text).toBe("> - ```\n>   ** not bold**\n>   ```\n");
    expect(block.fence).toEqual({
      open: false,
      run: "```",
      depth: 1,
      closeLine: 26,
      prefix: ">   ",
      lines: [4, 12, 30],
    });
    const runs = styledRuns(text);
    // The body is code, never bold; the list marker stays plain.
    expect(runs.filter(([, name]) => name.includes("bold")).map(([t]) => t)).toEqual(["b"]);
    expect(runs).toContainEqual([">   ** not bold**\n".slice(1), "fence"]);
    expect(runs.find(([t]) => t === "-")).toBeUndefined();
    expect(rest.fence).toBeUndefined();
    expect(reassemble(text)).toBe(text);
  });

  it("gives unchanged fence lines the prefix of the containers around them now", () => {
    const lines = ">   ```\n>   code\n>   ```";
    const prefixOf = (draft: string) =>
      tokenizeComposerMarkdown(draft).blocks.find((block) => block.fence)?.fence?.prefix;
    // Parsed first alone (a quote's fence), then inside a list item opened
    // above it, then alone again: each keeps its own containers.
    expect(prefixOf(lines)).toBe("> ");
    expect(prefixOf(`> - item\n${lines}`)).toBe(">   ");
    expect(prefixOf(lines)).toBe("> ");
    expect(prefixOf(`> - item\n${lines}`)).toBe(">   ");
  });

  it("finds fences after any list and quote containers", () => {
    const fence = (text: string) => tokenizeComposerMarkdown(text).blocks[0].fence;
    expect(fence("- ```\n  ** x**\n  ```")?.prefix).toBe("  ");
    expect(fence("1. ```\n   code\n   ```")?.prefix).toBe("   ");
    expect(fence("> > - ```\n> >   code\n> >   ```")?.prefix).toBe("> >   ");
    expect(fence("- > ```\n  > code\n  > ```")?.prefix).toBe("  > ");
    expect(fence(">- ```\n>   code\n>   ```")?.prefix).toBe(">   ");
    // An ordered item not from 1 can't interrupt a paragraph: no fence there.
    const after = tokenizeComposerMarkdown("para\n2. ```\ncode **b**").blocks;
    expect(after.some((block) => block.fence)).toBe(false);
  });

  it("ends a list item's fence where the item ends, and reopens in the item", () => {
    const blocks = tokenizeComposerMarkdown("- ```\n  a\n  ```\n  ```\n  b\nafter **b**").blocks;
    expect(blocks.map((block) => [block.text, block.fence?.prefix])).toEqual([
      ["- ```\n  a\n  ```\n", "  "],
      ["  ```\n  b\n", "  "],
      ["after **b**", undefined],
    ]);
    expect(styledRuns("- ```\n  code\nafter **b**")).toContainEqual(["b", "bold"]);
    // A quote inside the item ends before the item does.
    const inner = tokenizeComposerMarkdown("- > ```\n  ```\nafter").blocks;
    expect(inner.map((block) => [block.text, block.fence?.prefix])).toEqual([
      ["- > ```\n", "  > "],
      ["  ```\n", "  "],
      ["after", undefined],
    ]);
  });

  it("tiles the draft exactly with the new styles", () => {
    for (const text of ["~~a~~ [b](c) > d", "> [l](u) ~s~\nlazy **b**", "x [a](<b>) ~~c~~"])
      expect(reassemble(text)).toBe(text);
  });
});

describe("markerRevealRanges", () => {
  /** Each revealed marker as `text@offset`, in draft order. */
  function revealed(text: string, start: number, end = start): string[] {
    return markerRevealRanges(tokenizeComposerMarkdown(text), start, end).map(
      (range) => `${text.slice(range.start, range.end)}@${range.start}`,
    );
  }
  const draft = "say _hi_ and **bold** then `code` end";
  const at = (needle: string) => draft.indexOf(needle);

  it("reveals the markers of the token the caret is inside", () => {
    expect(revealed(draft, at("hi") + 1)).toEqual(["_@4", "_@7"]);
    expect(revealed(draft, at("old"))).toEqual(["**@13", "**@19"]);
    expect(revealed(draft, at("ode"))).toEqual(["`@27", "`@32"]);
  });

  it("reveals a token when the caret sits right beside either marker", () => {
    expect(revealed(draft, at("_hi_"))).toEqual(["_@4", "_@7"]);
    expect(revealed(draft, at("_hi_") + 4)).toEqual(["_@4", "_@7"]);
    expect(revealed(draft, at("**bold**") + 8)).toEqual(["**@13", "**@19"]);
    expect(revealed(draft, at("`code`") + 6)).toEqual(["`@27", "`@32"]);
  });

  it("hides every marker when the caret is away from tokens", () => {
    expect(revealed(draft, 0)).toEqual([]);
    expect(revealed(draft, at("_hi_") - 1)).toEqual([]);
    expect(revealed(draft, at("_hi_") + 5)).toEqual([]);
    expect(revealed(draft, draft.length)).toEqual([]);
  });

  it("reveals every token a selection overlaps", () => {
    expect(revealed(draft, at("hi"), at("bold"))).toEqual(["_@4", "_@7", "**@13", "**@19"]);
    expect(revealed(draft, 0, draft.length)).toHaveLength(6);
    expect(revealed(draft, at(" and") + 1, at(" then") + 1)).toEqual(["**@13", "**@19"]);
  });

  it("reveals adjacent tokens separately, both only at their shared edge", () => {
    const text = "_a_**b**";
    expect(revealed(text, 1)).toEqual(["_@0", "_@2"]);
    expect(revealed(text, 5)).toEqual(["**@3", "**@6"]);
    expect(revealed(text, 3)).toEqual(["_@0", "_@2", "**@3", "**@6"]);
  });

  it("reveals a nested token's markers only when the caret touches it", () => {
    const text = "x **bold _and_ `code`** y";
    const outer = [`**@2`, `**@21`];
    expect(revealed(text, text.indexOf("old"))).toEqual(outer);
    expect(revealed(text, text.indexOf("nd"))).toEqual(["**@2", "_@9", "_@13", "**@21"]);
    expect(revealed(text, text.indexOf("ode"))).toEqual(["**@2", "`@15", "`@20", "**@21"]);
  });

  it("reveals every token of one delimiter run together", () => {
    // `***` opens both the italic and the bold; touching the run shows both.
    expect(revealed("***x***", 0)).toEqual(["*@0", "**@1", "**@4", "*@6"]);
  });

  it("reveals only a fence's backticks, from anywhere inside the block", () => {
    const text = "intro\n```ts\nconst a = 1;\n```\nafter";
    const fences = ["```@6", "```@25"];
    const fenceStart = text.indexOf("```");
    expect(revealed(text, fenceStart)).toEqual(fences);
    expect(revealed(text, fenceStart + 5)).toEqual(fences);
    expect(revealed(text, text.indexOf("a = 1"))).toEqual(fences);
    // The end of the closing fence line, but not the start of the next line.
    expect(revealed(text, text.indexOf("after") - 1)).toEqual(fences);
    expect(revealed(text, text.indexOf("after"))).toEqual([]);
    expect(revealed(text, 2)).toEqual([]);
  });

  it("never reveals an unclosed fence: its backticks are always shown", () => {
    const text = "intro\n```\nstill typing\n";
    expect(revealed(text, text.length)).toEqual([]);
    expect(revealed(text, text.indexOf("typing"))).toEqual([]);
  });

  it("finds the token beside the caret at a paragraph boundary", () => {
    const text = "**a**\n\n_b_";
    expect(revealed(text, 5)).toEqual(["**@0", "**@3"]);
    expect(revealed(text, 6)).toEqual([]);
    expect(revealed(text, 7)).toEqual(["_@7", "_@9"]);
  });

  it("reveals a code span crossing lines of a long paragraph from either line", () => {
    const filler = Array.from({ length: MAX_PARAGRAPH_LINES + 1 }, () => "filler");
    const text = ["`multi **not bold**", "line` **yes**", ...filler].join("\n");
    const code = ["`@0", "`@24"];
    expect(revealed(text, 3)).toEqual(code);
    expect(revealed(text, text.indexOf("line"))).toEqual(code);
    expect(revealed(text, text.indexOf("yes"))).toEqual(["**@26", "**@31"]);
  });

  it("reveals strikethrough, link and quote markers per token", () => {
    const text = "x ~~s~~ [l](u) y";
    expect(revealed(text, text.indexOf("s~"))).toEqual(["~~@2", "~~@5"]);
    // A link's url is always shown; only its brackets reveal.
    expect(revealed(text, text.indexOf("l]"))).toEqual(["[@8", "](@10", ")@13"]);
    expect(revealed(text, 1)).toEqual([]);
    const quote = "> one\n> two\nlazy";
    expect(revealed(quote, 3)).toEqual([">@0"]);
    expect(revealed(quote, quote.indexOf("two"))).toEqual([">@6"]);
    expect(revealed(quote, quote.indexOf("lazy") + 1)).toEqual([]);
  });

  it("stays local on a long draft", () => {
    const text = Array.from({ length: 5000 }, (_, i) => `line ${i} with _em_ **b**`).join("\n");
    const markdown = tokenizeComposerMarkdown(text);
    const caret = text.indexOf("_em_", text.length / 2) + 1;
    const started = performance.now();
    for (let i = 0; i < 1000; i++) markerRevealRanges(markdown, caret, caret);
    expect(performance.now() - started).toBeLessThan(200);
    expect(markerRevealRanges(markdown, caret, caret)).toHaveLength(2);
  });
});
