import { SearchQuery } from "@codemirror/search";
import { Text } from "@codemirror/state";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MatchCollector } from "./findMatchCollector";
import { foldedText, NOTHING_FOUND, plainMatches } from "./plainTextMatches";

interface Options {
  caseSensitive: boolean;
  wholeWord: boolean;
}

const OPTIONS: Options[] = [false, true].flatMap((caseSensitive) =>
  [false, true].map((wholeWord) => ({ caseSensitive, wholeWord })),
);

function docOf(text: string): Text {
  return Text.of(text.split("\n"));
}

function cursorMatches(query: SearchQuery, doc: Text, from = 0, to = doc.length) {
  return [
    ...(query.getCursor(doc, from, to) as unknown as Iterable<{
      from: number;
      to: number;
      precise?: boolean;
    }>),
  ].map(({ from: start, to: end, precise }) => ({
    from: start,
    to: end,
    precise: precise !== false,
  }));
}

function fastMatches(query: SearchQuery, doc: Text, from = 0, to = doc.length) {
  const cursor = plainMatches(query, doc, from, to);
  if (!cursor) return null;
  return [...{ [Symbol.iterator]: () => cursor }].filter((match) => match !== NOTHING_FOUND);
}

/** A small seeded generator, so a failure names the case that broke. */
function random(seed: number) {
  let state = seed;
  return (below: number) => {
    state = (Math.imul(state, 1103515245) + 12345) & 0x7fffffff;
    // The low bits of this generator cycle quickly, so choices read the high ones.
    return (state >>> 15) % below;
  };
}

// Characters that fold to themselves, or one-for-one, so the fast path applies.
const ONE_FOR_ONE = [
  "a",
  "A",
  "b",
  "B",
  "_",
  "1",
  " ",
  "\n",
  "-",
  "Σ",
  "σ",
  "ς",
  "Ａ",
  "ж",
  "Ж",
  "😀",
  // A combining acute accent, which folds to itself.
  "e\u0301",
];
// Characters whose fold changes length or crosses an astral boundary: the cursor's job.
// é, İ, ß, ﬁ, the Kelvin sign, a Deseret capital, ǅ, and 🆐 (which folds to "dj").
const FOLD_CHANGING = [
  "\u00e9",
  "\u0130",
  "\u00df",
  "\ufb01",
  "\u212a",
  "\u{10400}",
  "\u01c5",
  "\u{1f190}",
];

function sample(pick: (below: number) => number, alphabet: string[], length: number) {
  return Array.from({ length }, () => alphabet[pick(alphabet.length)]!).join("");
}

// The search ranges each case may take: the whole document, a prefix, a suffix, or inside it.
const RANGE_KINDS = ["document", "prefix", "suffix", "interior"] as const;

function rangeOf(
  kind: (typeof RANGE_KINDS)[number],
  pick: (below: number) => number,
  length: number,
): { from: number; to: number } {
  if (kind === "document") return { from: 0, to: length };
  if (kind === "prefix") return { from: 0, to: pick(length + 1) };
  if (kind === "suffix") return { from: pick(length + 1), to: length };
  const from = pick(length + 1);
  return { from, to: from + pick(length - from + 1) };
}

describe("plainMatches", () => {
  it("matches CodeMirror's cursor exactly, whatever the text and options", () => {
    let fastRuns = 0;
    let wholeWordRuns = 0;
    const rangeRuns = new Map<string, number>();
    for (let seed = 1; seed <= 1000; seed += 1) {
      const pick = random(seed);
      const alphabet =
        seed % 4 === 0
          ? [...ONE_FOR_ONE, ...FOLD_CHANGING]
          : seed % 2 === 0
            ? ONE_FOR_ONE
            : ONE_FOR_ONE.slice(0, 9);
      const text = sample(pick, alphabet, 20 + pick(80));
      const doc = docOf(text);
      const start = pick(text.length);
      const searches = [
        text.slice(start, start + 1 + pick(4)),
        sample(pick, alphabet, 1 + pick(3)),
        "a\\nb",
        "ab",
      ];
      for (const search of searches) {
        for (const options of OPTIONS) {
          const query = new SearchQuery({ search, ...options });
          if (!query.valid) continue;
          const kind = RANGE_KINDS[pick(RANGE_KINDS.length)]!;
          const { from, to } = rangeOf(kind, pick, doc.length);
          const fast = fastMatches(query, doc, from, to);
          if (fast === null) continue;
          fastRuns += 1;
          rangeRuns.set(kind, (rangeRuns.get(kind) ?? 0) + 1);
          if (options.wholeWord) wholeWordRuns += 1;
          expect(fast, JSON.stringify({ seed, text, search, options, from, to })).toEqual(
            cursorMatches(query, doc, from, to),
          );
        }
      }
    }
    // The fast path ran for most cases, so the comparison means something.
    expect(fastRuns).toBeGreaterThan(8000);
    expect(wholeWordRuns).toBeGreaterThan(2000);
    for (const kind of RANGE_KINDS) expect(rangeRuns.get(kind), kind).toBeGreaterThan(1500);
  });

  it.each([
    ["case folding", "Alpha ALPHA alpha", "alpha", { caseSensitive: false, wholeWord: false }, 3],
    [
      "a case-sensitive query",
      "Alpha ALPHA alpha",
      "alpha",
      { caseSensitive: true, wholeWord: false },
      1,
    ],
    ["whole words", "cat cats _cat cat_ cat", "cat", { caseSensitive: false, wholeWord: true }, 2],
    ["non-overlapping repeats", "aaaaa", "aa", { caseSensitive: false, wholeWord: false }, 2],
    [
      "a word match inside a rejected one",
      "aa a a",
      "a a",
      { caseSensitive: false, wholeWord: true },
      1,
    ],
    ["escapes", "a\\b\ta\\b", "a\\\\b\\t", { caseSensitive: false, wholeWord: false }, 1],
    [
      "a query across lines",
      "one\ntwo\none\ntwo",
      "one\\ntwo",
      { caseSensitive: false, wholeWord: false },
      2,
    ],
    [
      "a final sigma in the query",
      "ΟΔΟΣ οδος οδοσ",
      "ΟΔΟΣ",
      { caseSensitive: false, wholeWord: false },
      1,
    ],
  ] as const)("agrees with the cursor on %s", (_label, text, search, options, count) => {
    const doc = docOf(text);
    const query = new SearchQuery({ search, ...options });
    const fast = fastMatches(query, doc);
    expect(fast).not.toBeNull();
    expect(fast).toEqual(cursorMatches(query, doc));
    expect(fast).toHaveLength(count);
  });

  it.each([
    ["a regular expression", "alpha", "a.p", { regexp: true }],
    ["a decomposing character", "café café", "cafe", {}],
    ["a dotted capital I", "İstanbul", "i", {}],
    ["a whole word next to non-ASCII text", "naжve na", "na", { wholeWord: true }],
    ["a query with a lone surrogate", "😀", "\uD83D", { caseSensitive: true }],
    ["an astral character that folds to two letters", "\u{1F190}", "j", {}],
  ])("leaves %s to CodeMirror's cursor", (_label, text, search, options) => {
    const query = new SearchQuery({ search, ...options });
    expect(plainMatches(query, docOf(text), 0, text.length)).toBeNull();
  });

  it("folds a document once per case setting and keeps it for that version", () => {
    const doc = docOf("Alpha\nΣ beta");
    const normalize = vi.spyOn(String.prototype, "normalize");
    try {
      expect(foldedText(doc, false)).toBe("alpha\nσ beta");
      const folds = normalize.mock.calls.length;
      expect(folds).toBeGreaterThan(0);
      expect(foldedText(doc, false)).toBe("alpha\nσ beta");
      expect(normalize).toHaveBeenCalledTimes(folds);
      expect(foldedText(doc, true)).toBe("Alpha\nΣ beta");
      expect(normalize.mock.calls.length).toBeGreaterThan(folds);
    } finally {
      normalize.mockRestore();
    }
    expect(foldedText(docOf("İ"), false)).toBeNull();
  });

  const lines = Array.from(
    { length: 20_000 },
    (_, index) => `export const entry${index} = { label: 'row ${index}' };`,
  ).join("\n");

  // Long inputs of each shape the fast path is for; how much it reads is bounded below.
  it.each([
    ["sparse matches in many lines", lines, "ROW 19999'", false, 1],
    ["whole words on one long line", "cat ".repeat(20_000), "cat", true, 20_000],
    ["turned-down words on one long line", "cats ".repeat(20_000), "cat", true, 0],
  ])("counts %s as CodeMirror's cursor does", (_label, text, search, wholeWord, count) => {
    const doc = docOf(text);
    const collector = new MatchCollector(doc, {
      search,
      caseSensitive: false,
      wholeWord,
      regexp: false,
    });
    const { froms } = collector.step(60_000, () => performance.now());
    const cursor = cursorMatches(new SearchQuery({ search, wholeWord }), doc);
    expect(froms).toHaveLength(count);
    expect(froms).toEqual(cursor.map((match) => match.from));
  });

  describe("on a long line dense with whole-word candidates", () => {
    afterEach(() => vi.restoreAllMocks());

    it("reads the text a bounded number of times", () => {
      const text = "cat cats ".repeat(10_000);
      const doc = docOf(text);
      foldedText(doc, false);
      let scanned = 0;
      const { indexOf } = String.prototype;
      vi.spyOn(String.prototype, "indexOf").mockImplementation(function (
        this: string,
        needle,
        from,
      ) {
        const found = indexOf.call(this, needle, from);
        scanned += (found < 0 ? this.length : found) - (from ?? 0);
        return found;
      });
      const matches = fastMatches(new SearchQuery({ search: "cat", wholeWord: true }), doc);
      vi.restoreAllMocks();
      expect(matches).toHaveLength(10_000);
      expect(scanned).toBeLessThan(10 * text.length);
    });

    // Every other line of a long file scoped, as a column selection does, and a literal absent.
    function scopedLines(count: number) {
      const doc = docOf(Array.from({ length: count }, (_, index) => `line ${index}`).join("\n"));
      const scope = Array.from({ length: count / 2 }, (_, index) => {
        const line = doc.line(index * 2 + 1);
        return { from: line.from, to: line.to };
      });
      return { doc, scope };
    }

    it("reads each scoped line only, not the rest of the file after it", () => {
      const { doc, scope } = scopedLines(4_000);
      const text = doc.toString();
      foldedText(doc, true);
      let scanned = 0;
      const { indexOf } = String.prototype;
      vi.spyOn(String.prototype, "indexOf").mockImplementation(function (
        this: string,
        needle,
        from,
      ) {
        const found = indexOf.call(this, needle, from);
        scanned += (found < 0 ? this.length : found) - (from ?? 0);
        return found;
      });
      const collector = new MatchCollector(doc, {
        search: "absent",
        caseSensitive: true,
        wholeWord: false,
        regexp: false,
        scope,
      });
      const batch = collector.step(60_000, () => 0);
      vi.restoreAllMocks();
      expect(batch).toMatchObject({ froms: [], done: true });
      expect(scanned).toBeLessThan(2 * text.length);
    });

    it("lets a time-boxed step pause between scoped lines that match nothing", () => {
      const { doc, scope } = scopedLines(400);
      const collector = new MatchCollector(doc, {
        search: "absent",
        caseSensitive: true,
        wholeWord: false,
        regexp: false,
        scope,
      });
      let time = 0;
      const expiring = () => (time += 100);
      let steps = 0;
      while (!collector.step(1, expiring).done) steps += 1;
      expect(steps).toBeGreaterThanOrEqual(scope.length - 1);
    });

    it("lets a time-boxed step pause after each candidate it turns down", () => {
      const collector = new MatchCollector(docOf("cats ".repeat(10_000)), {
        search: "cat",
        caseSensitive: false,
        wholeWord: true,
        regexp: false,
      });
      let time = 0;
      // A clock that passes any budget on each read: one candidate per step.
      const expiring = () => (time += 100);
      expect(collector.step(8, expiring)).toMatchObject({ froms: [], done: false });
      let steps = 1;
      while (!collector.step(8, expiring).done) steps += 1;
      expect(steps).toBeGreaterThanOrEqual(10_000);
    });
  });
});
