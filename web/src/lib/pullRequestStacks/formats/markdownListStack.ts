// A stack section written as a Markdown list under one heading, as stack tools write it at the
// top of each PR. Output matching the producer exactly verifies; hand edits serve navigation only.

import { markdownLines, type MarkdownLine } from "@/lib/pullRequestStacks/markdownLines";
import {
  resolveStackOutline,
  type StackFormat,
  type StackLandingTool,
  type StackOutlineEntry,
  type StackParse,
  type StackSelf,
  type StackTolerance,
} from "@/lib/pullRequestStacks/stackModel";

export interface MarkdownListStackSyntax {
  id: string;
  /** The heading line that opens the section. */
  header: string;
  /** What the producer may append to the heading; anything else reads as a hand edit. */
  headerSuffix?: string;
  /** A line the producer may write between the heading and the entries. */
  introLine?: RegExp;
  landing?: StackLandingTool;
}

const FOOTER = /^-{9}[ \t]*$/;
const RULE = /^ {0,3}-{3,}[ \t]*$/;
// Dot-all, since git allows U+2028 and U+2029 in a branch name and `.` would stop at them.
const BULLET = /^([ \t]*)- (.*)$/s;
const WORD_START = /^[\p{L}\p{N}_]/u;
// Git forbids ASCII spaces and `[` in branch names, so a label never runs into the text
// after it; other Unicode spaces are legal and stay part of the name.
const LINKED_LABEL = /^\[([^ \t[]+?)\]\(([^ \t()]+)\)/;
const BRACKETED_LABEL = /^\[([^ \t[]+?)\]/;
// Characters git never allows in a branch name, besides ASCII control characters.
const FORBIDDEN_IN_BRANCH = [" ", "~", "^", ":", "?", "*", "[", "\\"];

/** Whether `name` could be a branch git created; anything else isn't producer output. */
function isBranchName(name: string): boolean {
  // `git check-ref-format --branch` also refuses `HEAD` and a name starting with `-`.
  if (name === "" || name === "HEAD" || name.startsWith("-")) return false;
  if (name.includes("..") || name.includes("@{")) return false;
  // Git's rules for each `/`-separated part, and for the name's ends.
  if (name.endsWith(".") || name.split("/").some((part) => /^$|^\.|\.lock$/.test(part))) {
    return false;
  }
  return ![...name].some(
    (character) =>
      FORBIDDEN_IN_BRANCH.includes(character) ||
      (character.codePointAt(0) ?? 0) < 0x20 ||
      character === "\u007f",
  );
}
const BOLD = /^\*\*(.+)\*\*$/s;
// What the producer appends after an entry's label, in its order.
const PRODUCER_SUFFIX =
  /^(?: \[\[Files changed\]\([^)\s]+\)\])?( ⬆️? \(dependency: merge first\))?( \[MERGED\])?( ← _this PR_)?$/u;
const DEPENDENCY = /⬆️? \(dependency: merge first\)/u;

interface ReadEntry {
  entry: StackOutlineEntry;
  dependency: boolean;
  /** Tab indentation, which the producer never writes. */
  tabbed: boolean;
}

function readEntry(
  indentText: string,
  content: string,
  tolerate: (reason: StackTolerance) => void,
): ReadEntry {
  let label: string;
  let url: string | null = null;
  let rest: string;
  const linked = LINKED_LABEL.exec(content);
  const bracketed = linked ? null : BRACKETED_LABEL.exec(content);
  if (linked) {
    [, label = "", url = null] = linked;
    rest = content.slice(linked[0].length);
  } else if (bracketed) {
    // A bracketed branch without a link isn't producer output; read it as unpushed.
    tolerate("link");
    label = bracketed[1] ?? "";
    rest = content.slice(bracketed[0].length);
  } else {
    const space = content.indexOf(" ");
    label = space === -1 ? content : content.slice(0, space);
    rest = space === -1 ? "" : content.slice(space);
  }

  const bold = BOLD.exec(label);
  const branch = bold?.[1] ?? label;
  // Kept as a placeholder node so the entries under it keep their parent.
  if (!isBranchName(branch)) tolerate("unexpected-line");
  const suffix = PRODUCER_SUFFIX.exec(rest);
  if (!suffix) tolerate("unexpected-line");
  const dependency = suffix ? suffix[1] !== undefined : DEPENDENCY.test(rest);
  const merged = suffix ? suffix[2] !== undefined : rest.includes("[MERGED]");
  const pointed = suffix ? suffix[3] !== undefined : rest.includes("← _this PR_");
  // The producer bolds the current branch and points at it together.
  if (Boolean(bold) !== pointed) tolerate("current-marker");

  return {
    entry: {
      indent: [...indentText].reduce((width, character) => width + (character === "\t" ? 4 : 1), 0),
      branch,
      url,
      markers: { current: Boolean(bold) || pointed, merged },
    },
    dependency,
    tabbed: indentText.includes("\t"),
  };
}

/**
 * Where the section ends: the producer's footer, or for a hand-edited body, the first blank line
 * after the entries or the first line that isn't an entry. The producer writes no blank line
 * between entries, so a list of the user's own after one isn't read as more of the stack.
 */
function sectionEnd(
  lines: readonly MarkdownLine[],
  first: number,
  footerIndex: number,
): { end: number; tolerance: StackTolerance | null } {
  if (footerIndex !== -1) return { end: footerIndex, tolerance: null };
  let readEntries = false;
  for (let index = first; index < lines.length; index++) {
    const text = lines[index]?.text ?? "";
    if (text.trim() === "") {
      if (readEntries) return { end: index, tolerance: "footer" };
      continue;
    }
    if (BULLET.test(text)) {
      readEntries = true;
      continue;
    }
    return { end: index, tolerance: "footer" };
  }
  return { end: lines.length, tolerance: "footer" };
}

/**
 * The stack format for `syntax`. A producer's section looks like this, under its own heading:
 *
 *     ## Stack
 *     - [stack/dependency](<pr>) [[Files changed](<diff>)] ⬆️ (dependency: merge first)
 *       - [stack/base](<pr>) [[Files changed](<diff>)] [MERGED]
 *         - [**stack/current**](<pr>) [[Files changed](<diff>)] ← _this PR_
 *         - stack/unpushed
 *
 *     ---------
 *     <the user's description>
 */
export function markdownListStackFormat(syntax: MarkdownListStackSyntax): StackFormat {
  const { id, header: heading, headerSuffix, landing } = syntax;
  // Without `g` or `y`, `test` keeps no state between parses.
  const introLine = syntax.introLine
    ? new RegExp(syntax.introLine.source, syntax.introLine.flags.replace(/[gy]/g, ""))
    : undefined;
  // The heading as a whole word, so `## Stack` never claims a `## Stacked changes` section.
  const isHeader = (line: MarkdownLine) =>
    !line.fenced &&
    !line.quoted &&
    line.text.startsWith(heading) &&
    !WORD_START.test(line.text.slice(heading.length));

  function parse(body: string, self: StackSelf): StackParse | null {
    const lines = markdownLines(body);
    const headers = lines.flatMap((line, index) => (isHeader(line) ? [index] : []));
    // Two stack sections can't both describe this PR.
    if (headers.length !== 1) return null;
    const headerIndex = headers[0] as number;
    const header = lines[headerIndex] as MarkdownLine;

    const tolerances = new Set<StackTolerance>();
    const tolerate = (reason: StackTolerance) => tolerances.add(reason);
    const headerRest = header.text.slice(heading.length);
    if (headerRest !== "" && headerRest !== headerSuffix) tolerate("header");

    const footerIndex = lines.findIndex(
      (line, index) =>
        index > headerIndex && !line.fenced && !line.quoted && FOOTER.test(line.text),
    );
    // A footer that only appears above the header belongs to something else.
    if (
      footerIndex === -1 &&
      lines.some(
        (line, index) =>
          index < headerIndex && !line.fenced && !line.quoted && FOOTER.test(line.text),
      )
    ) {
      return null;
    }

    let first = headerIndex + 1;
    if (introLine?.test(lines[first]?.text ?? "")) first++;
    const { end, tolerance } = sectionEnd(lines, first, footerIndex);
    if (tolerance) tolerate(tolerance);

    const read: ReadEntry[] = [];
    for (let index = first; index < end; index++) {
      const line = lines[index] as MarkdownLine;
      if (line.text.trim() === "") {
        if (read.length === 0) continue;
        // The producer writes no blank line between entries, only one before its footer, so
        // anything after a blank line is the user's own and keeps the section from verifying.
        if (lines.slice(index, end).some((rest) => rest.text.trim() !== "")) {
          tolerate("unexpected-line");
        }
        break;
      }
      const bullet = BULLET.exec(line.text);
      // Entries end at the first other line, so a later rule can't pull the user's own lists in.
      if (!bullet || line.fenced || line.quoted) {
        tolerate(RULE.test(line.text) ? "footer" : "unexpected-line");
        break;
      }
      read.push(readEntry(bullet[1] ?? "", bullet[2] ?? "", tolerate));
    }
    if (read.length === 0) return null;

    // The producer lists the stack's dependency first and indents the whole stack under it.
    const leadsWithDependency = read[0]?.dependency === true;
    const dependency = leadsWithDependency ? (read[0]?.entry ?? null) : null;
    const members = leadsWithDependency ? read.slice(1) : read;
    if (members.length === 0) return null;
    if (members.some((item) => item.dependency)) tolerate("dependency");

    // The producer indents two spaces per level and never skips one.
    let level = -1;
    read.forEach((item, index) => {
      const itemLevel = item.entry.indent / 2;
      const minimum = leadsWithDependency && index > 0 ? 1 : 0;
      if (
        item.tabbed ||
        !Number.isInteger(itemLevel) ||
        itemLevel > level + 1 ||
        itemLevel < minimum
      ) {
        tolerate("indentation");
      }
      level = itemLevel;
    });

    // The producer marks the current PR only when there's something to tell it apart from.
    const needsMarker = members.length > 1 || dependency !== null;
    const marked = members.filter((item) => item.entry.markers.current).length;
    if (needsMarker ? marked !== 1 : marked !== 0) tolerate("current-marker");

    const footer = footerIndex === -1 ? null : lines[footerIndex];
    return resolveStackOutline(
      {
        format: id,
        dependency,
        members: members.map((item) => item.entry),
        span: footer ? { start: header.start, end: footer.end } : null,
        tolerances: [...tolerances],
      },
      self,
    );
  }

  return landing ? { id, parse, landing } : { id, parse };
}
