// Raw-text-aware HTML tag walk shared by comment-to-slide mapping and preview
// markup injection. Skips comments and script/style/textarea/title contents so
// tag-like strings inside them are never mistaken for real structure.

/** Tags whose contents are raw text (not parsed as HTML) until their close tag. */
export const RAW_TEXT_TAGS = new Set(["script", "style", "textarea", "title"]);

const VOID_TAGS = new Set([
  "area",
  "base",
  "br",
  "col",
  "embed",
  "hr",
  "img",
  "input",
  "link",
  "meta",
  "param",
  "source",
  "track",
  "wbr",
]);

/** Whitespace as the HTML tokenizer defines it. */
const TOKENIZER_SPACE = " \t\n\f\r";

/** Characters that end a tag name in the tokenizer: whitespace, `/` and `>`. */
const TAG_NAME_END = `${TOKENIZER_SPACE}/>`;

/** Whether `html` has the tag opener `name` (e.g. `</script`) at `at`. */
function hasTagAt(html: string, at: number, name: string): boolean {
  const next = html.charAt(at + name.length);
  return (
    next !== "" &&
    TAG_NAME_END.includes(next) &&
    html.slice(at, at + name.length).toLowerCase() === name
  );
}

/**
 * Index just past the `>` that ends the tag whose name ends at `from`, or -1 when
 * the tag is still open at end of input.
 */
function tagEnd(html: string, from: number): number {
  let state: "name" | "beforeValue" | "unquoted" = "name";
  for (let i = from; i < html.length; i++) {
    const c = html.charAt(i);
    if (c === ">") return i + 1;
    if (state === "beforeValue" && (c === '"' || c === "'")) {
      const close = html.indexOf(c, i + 1);
      if (close === -1) return -1;
      i = close;
      state = "name";
    } else if (TOKENIZER_SPACE.includes(c)) {
      if (state === "unquoted") state = "name";
    } else if (state === "name") {
      if (c === "=") state = "beforeValue";
    } else {
      state = "unquoted";
    }
  }
  return -1;
}

/**
 * Index just past the end tag that closes a `<script>` whose start tag ended at
 * `from`, or -1 when still open. Honors script escaped / double-escaped states.
 */
function scriptEnd(html: string, from: number): number {
  let state: "data" | "escaped" | "double" = "data";
  for (let i = from; i < html.length; i++) {
    const c = html.charAt(i);
    if (c === "-" && state !== "data") {
      let j = i + 1;
      while (html.charAt(j) === "-") j++;
      if (j - i >= 2 && html.charAt(j) === ">") {
        state = "data";
        i = j;
      } else {
        i = j - 1;
      }
    } else if (c !== "<") {
      continue;
    } else if (state === "data" && html.startsWith("<!--", i)) {
      let j = i + 4;
      while (html.charAt(j) === "-") j++;
      if (html.charAt(j) === ">") {
        i = j;
      } else {
        state = "escaped";
        i += 3;
      }
    } else if (hasTagAt(html, i, "</script")) {
      if (state !== "double") return tagEnd(html, i + 8);
      state = "escaped";
      i += 7;
    } else if (state === "escaped" && hasTagAt(html, i, "<script")) {
      state = "double";
      i += 6;
    }
  }
  return -1;
}

/** Advance past an HTML comment starting at `i`, or to `limit` if unclosed. */
function skipHtmlComment(html: string, i: number, limit: number): number {
  // Empty comments may close abruptly (`<!-->`, `<!--->`); else `-->` or `--!>`.
  let j = i + 4;
  while (html.charAt(j) === "-") j++;
  if (html.charAt(j) === ">") {
    return Math.min(j + 1, limit);
  }
  const terminator = /--!?>/g;
  terminator.lastIndex = i + 4;
  const match = terminator.exec(html);
  if (!match || match.index >= limit) return limit;
  return match.index + match[0].length;
}

/**
 * Advance past a raw-text element whose open tag starts at `openAt`. Contents
 * are not scanned for nested tags.
 */
function skipRawTextElement(html: string, openAt: number, name: string, limit: number): number {
  if (!hasTagAt(html, openAt, `<${name}`)) return Math.min(openAt + 1, limit);
  const openEnd = tagEnd(html, openAt + 1 + name.length);
  if (openEnd === -1 || openEnd > limit) return limit;
  // Self-closing raw-text tags are unusual but terminate at the open tag.
  if (html.slice(openAt, openEnd).includes("/>") || VOID_TAGS.has(name)) {
    return openEnd;
  }
  if (name === "script") {
    const close = scriptEnd(html, openEnd);
    return close === -1 || close > limit ? limit : close;
  }
  for (let i = openEnd; i < limit; i++) {
    if (!hasTagAt(html, i, `</${name}`)) continue;
    const end = tagEnd(html, i + 2 + name.length);
    return end === -1 || end > limit ? limit : end;
  }
  return limit;
}

/** Read a tag name starting after `<` or `</` at `nameAt`. */
function readTagName(html: string, nameAt: number): string | null {
  if (!/[a-zA-Z]/.test(html.charAt(nameAt))) return null;
  let j = nameAt + 1;
  while (j < html.length && /[\w:-]/.test(html.charAt(j))) j++;
  return html.slice(nameAt, j).toLowerCase();
}

/**
 * Walk HTML from `start`, skipping comments and raw-text element contents.
 * Invokes `onTag` for each real open/close tag; return a number to jump `i`,
 * or null to keep the default advance past that tag. Stops if a tag is still
 * open at end of input (parser drops the rest).
 */
export function walkHtmlTags(
  html: string,
  start: number,
  limit: number,
  onTag: (
    kind: "open" | "close",
    name: string,
    tagStart: number,
    tagStop: number,
    selfClosing: boolean,
  ) => number | null,
): void {
  let i = start;
  while (i < limit) {
    if (html.startsWith("<!--", i)) {
      i = skipHtmlComment(html, i, limit);
      continue;
    }
    if (html.charAt(i) !== "<") {
      i += 1;
      continue;
    }
    if (html.startsWith("</", i)) {
      const name = readTagName(html, i + 2);
      if (!name || !hasTagAt(html, i, `</${name}`)) {
        i += 1;
        continue;
      }
      const stop = tagEnd(html, i + 2 + name.length);
      if (stop === -1) return;
      const jump = onTag("close", name, i, stop, false);
      i = jump ?? stop;
      continue;
    }
    const name = readTagName(html, i + 1);
    if (!name || !hasTagAt(html, i, `<${name}`)) {
      i += 1;
      continue;
    }
    const stop = tagEnd(html, i + 1 + name.length);
    if (stop === -1) return;
    if (RAW_TEXT_TAGS.has(name)) {
      i = skipRawTextElement(html, i, name, limit);
      continue;
    }
    const selfClosing = /\/>$/.test(html.slice(i, stop)) || VOID_TAGS.has(name);
    const jump = onTag("open", name, i, stop, selfClosing);
    i = jump ?? stop;
  }
}

/**
 * Locate a real open or close tag by name, skipping raw-text contents and
 * comments. `last: true` keeps scanning for the final match.
 */
export function findTag(
  html: string,
  name: string,
  kind: "open" | "close",
  options?: { last?: boolean },
): { start: number; end: number } | null {
  const want = name.toLowerCase();
  let found: { start: number; end: number } | null = null;
  walkHtmlTags(html, 0, html.length, (k, n, tagStart, tagStop) => {
    if (k === kind && n === want) {
      found = { start: tagStart, end: tagStop };
      if (!options?.last) return html.length;
    }
    return null;
  });
  return found;
}
