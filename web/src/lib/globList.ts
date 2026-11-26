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

/** One step of a compiled glob, matched against a path one UTF-16 unit at a time. */
type GlobStep =
  /** Any one of these literal texts, already case-folded. */
  | { kind: "literal"; texts: string[] }
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

// The regex `i` flag's case folding, one UTF-16 unit at a time, so lengths never change.
function foldUnit(unit: string): string {
  const upper = unit.toUpperCase();
  if (upper.length !== 1) return unit;
  if (unit.charCodeAt(0) >= 128 && upper.charCodeAt(0) < 128) return unit;
  return upper;
}

function fold(text: string): string {
  let folded = "";
  for (let index = 0; index < text.length; index++) folded += foldUnit(text[index]!);
  return folded;
}

// What `.` leaves out, as the regex this matcher replaces did.
const LINE_TERMINATORS = new Set(["\n", "\r", "\u2028", "\u2029"]);

function globSteps(glob: string): GlobStep[] {
  const steps: GlobStep[] = [];
  const pushLiteral = (character: string) => {
    const last = steps.at(-1);
    if (last?.kind === "literal" && last.texts.length === 1) last.texts[0] += fold(character);
    else steps.push({ kind: "literal", texts: [fold(character)] });
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
          .map(fold),
      });
      index = close;
    } else {
      pushLiteral(character);
    }
  }
  return steps;
}

/** The positions in `path` one step can reach from the positions `from` marks. */
function advance(step: GlobStep, path: string, from: Uint8Array): Uint8Array<ArrayBuffer> {
  const length = path.length;
  const next = new Uint8Array(length + 1);
  if (step.kind === "literal") {
    for (let position = 0; position <= length; position++) {
      if (!from[position]) continue;
      for (const text of step.texts) {
        if (path.startsWith(text, position)) next[position + text.length] = 1;
      }
    }
  } else if (step.kind === "one") {
    for (let position = 0; position < length; position++) {
      if (from[position] && path[position] !== "/") next[position + 1] = 1;
    }
  } else {
    // A sweep: a run starts at any reached position and goes on until the step's stop.
    const stops =
      step.kind === "name"
        ? (character: string) => character === "/"
        : (character: string) => LINE_TERMINATORS.has(character);
    let running = false;
    for (let position = 0; position <= length; position++) {
      if (from[position]) running = true;
      if (running && (step.kind !== "folders" || from[position] || path[position - 1] === "/")) {
        next[position] = 1;
      }
      if (position < length && stops(path[position]!)) running = false;
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
      const folded = fold(path);
      let reached: Uint8Array<ArrayBuffer> = new Uint8Array(folded.length + 1);
      reached[0] = 1;
      for (const step of steps) {
        reached = advance(step, folded, reached);
        if (!reached.includes(1)) return false;
      }
      return reached[folded.length] === 1;
    },
  };
}
