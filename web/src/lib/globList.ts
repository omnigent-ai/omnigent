// Splits the comma-separated glob lists the file search fields take, exactly
// as the runner's `split_glob_list` does, so every search backend sees the
// same patterns.

// The characters Python's `str.strip()` removes (`str.isspace()`), which
// differ from JavaScript's `trim()`: it keeps U+FEFF and removes U+001C-U+001F
// and U+0085.
const PYTHON_WHITESPACE = new Set([
  ..."\t\n\v\f\r\u001c\u001d\u001e\u001f \u0085\u00a0\u1680",
  ..."\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a",
  ..."\u2028\u2029\u202f\u205f\u3000",
]);

function stripLikePython(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && PYTHON_WHITESPACE.has(text[start]!)) start += 1;
  while (end > start && PYTHON_WHITESPACE.has(text[end - 1]!)) end -= 1;
  return text.slice(start, end);
}

/**
 * Split a comma-separated glob list on top-level commas only.
 *
 * Commas inside a `{...}` alternation stay, so `"*.{js,ts}, src/**"` gives
 * `["*.{js,ts}", "src/**"]`. Entries are stripped as Python strips them, and
 * blank ones dropped.
 */
export function splitGlobList(raw: string): string[] {
  const patterns: string[] = [];
  let current = "";
  let depth = 0;
  for (const character of raw) {
    if (character === "{") depth += 1;
    else if (character === "}") depth = Math.max(0, depth - 1);
    else if (character === "," && depth === 0) {
      patterns.push(current);
      current = "";
      continue;
    }
    current += character;
  }
  patterns.push(current);
  return patterns.map(stripLikePython).filter((pattern) => pattern !== "");
}

/** One step of a compiled glob, matched against a path one code point at a time. */
type GlobStep =
  /** Any one of these literal texts, as case keys. */
  | { kind: "literal"; texts: string[][] }
  /** `?`: one character other than `/`. */
  | { kind: "one" }
  /** `*`: any run of characters other than `/`. */
  | { kind: "name" }
  /** `**` not followed by `/`: any run of characters. */
  | { kind: "any" }
  /** `**\/`: nothing, or any run of characters that ends in `/`. */
  | { kind: "folders" };

/** A compiled glob; `test` takes time linear in the path for each step, with no backtracking. */
export interface GlobMatcher {
  test: (path: string) => boolean;
}

// Pairs Python's `re.IGNORECASE` matches though their simple case mappings differ.
const EXTRA_CASES = new Map([
  ["\u1fd3", "\u0390"],
  ["\u1fe3", "\u03b0"],
  ["\ufb05", "\ufb06"],
]);

/**
 * A code point's key under Python's `re.IGNORECASE`: two code points match when their keys
 * are equal. That is the simple lowercase, then its uppercase when that is one code point.
 */
function caseKey(character: string): string {
  const lower = String.fromCodePoint(character.toLowerCase().codePointAt(0)!);
  const upper = lower.toUpperCase();
  const key = Array.from(upper).length === 1 ? upper : lower;
  return EXTRA_CASES.get(key) ?? key;
}

function caseKeys(text: string): string[] {
  return Array.from(text, caseKey);
}

function startsWithAt(path: string[], text: string[], position: number): boolean {
  if (position + text.length > path.length) return false;
  return text.every((character, offset) => path[position + offset] === character);
}

function globSteps(glob: string): GlobStep[] {
  const steps: GlobStep[] = [];
  const pushLiteral = (character: string) => {
    const last = steps.at(-1);
    if (last?.kind === "literal" && last.texts.length === 1)
      last.texts[0]!.push(caseKey(character));
    else steps.push({ kind: "literal", texts: [[caseKey(character)]] });
  };
  for (let index = 0; index < glob.length; index++) {
    const character = glob[index]!;
    if (character === "*" && glob[index + 1] === "*") {
      // `**/` can also match no folders, so `**/foo` matches a top-level `foo`.
      if (glob[index + 2] === "/") {
        // Repeated `**/` match what one does, outside braces only.
        if (steps.at(-1)?.kind !== "folders") steps.push({ kind: "folders" });
        index += 2;
      } else {
        steps.push({ kind: "any" });
        index += 1;
      }
    } else if (character === "*") {
      steps.push({ kind: "name" });
    } else if (character === "?") {
      steps.push({ kind: "one" });
    } else if (character === "{" && glob.indexOf("}", index) !== -1) {
      const close = glob.indexOf("}", index);
      // Braces take their alternatives literally, wildcards included.
      steps.push({
        kind: "literal",
        texts: glob
          .slice(index + 1, close)
          .split(",")
          .map(caseKeys),
      });
      index = close;
    } else {
      const codePoint = String.fromCodePoint(glob.codePointAt(index)!);
      pushLiteral(codePoint);
      index += codePoint.length - 1;
    }
  }
  return steps;
}

/** The positions in `path` one step can reach from the positions `from` marks. */
function advance(step: GlobStep, path: string[], from: Uint8Array): Uint8Array<ArrayBuffer> {
  const length = path.length;
  const next = new Uint8Array(length + 1);
  if (step.kind === "literal") {
    for (let position = 0; position <= length; position++) {
      if (!from[position]) continue;
      for (const text of step.texts) {
        if (startsWithAt(path, text, position)) next[position + text.length] = 1;
      }
    }
  } else if (step.kind === "one") {
    for (let position = 0; position < length; position++) {
      if (from[position] && path[position] !== "/") next[position + 1] = 1;
    }
  } else {
    // A sweep: a run starts at any reached position and goes on until the step's stop. The
    // runner's `**` is a regex dot, which stops only at a newline.
    const stop = step.kind === "name" ? "/" : "\n";
    let running = false;
    for (let position = 0; position <= length; position++) {
      if (from[position]) running = true;
      if (running && (step.kind !== "folders" || from[position] || path[position - 1] === "/")) {
        next[position] = 1;
      }
      if (position < length && path[position] === stop) running = false;
    }
  }
  return next;
}

/**
 * One glob as the runner's `_glob_to_regex` reads it, matched case-insensitively: `*` and
 * `?` stay within a name, `**` crosses folders, `{a,b}` takes either literal, a pattern
 * without `/` matches a name at any depth, and a trailing `/` covers everything below.
 */
export function globMatcher(pattern: string): GlobMatcher {
  let glob = pattern;
  if (!glob.includes("/")) glob = `**/${glob}`;
  else if (glob.endsWith("/")) glob = `${glob}**`;
  const steps = globSteps(glob);
  return {
    test(path: string): boolean {
      const keys = caseKeys(path);
      let reached: Uint8Array<ArrayBuffer> = new Uint8Array(keys.length + 1);
      reached[0] = 1;
      for (const step of steps) {
        reached = advance(step, keys, reached);
        if (!reached.includes(1)) return false;
      }
      // The runner's `$` also matches before a final newline.
      return reached[keys.length] === 1 || (keys.at(-1) === "\n" && reached[keys.length - 1] === 1);
    },
  };
}
