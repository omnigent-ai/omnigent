// The change a file-edit tool call made, as a unified diff with its added and
// removed line counts. Edit tools carry exact old/new replacement pairs; Codex's
// `apply_patch` carries a per-file diff from the harness; a full-file write
// carries only the new content.

import { stripOmnigentPrefix } from "@/lib/toolTitle";
import { firstLine, isRecord, parseToolOutput } from "@/lib/toolValue";

/** One file's part of an edit. */
export interface FileDiffSection {
  path: string;
  /** Where a rename moved the file; absent when it stays put. */
  newPath?: string;
  /** `write`: a whole file written over one whose previous content is not recorded. */
  kind: "update" | "add" | "delete" | "write";
  /** Diff lines: `+`, `-` and ` ` (context), with `@@` between hunks; a `write` is the plain content. */
  text: string;
  additions: number;
  removals: number;
}

export interface FileEditDiff {
  /** One section per file the edit touched. */
  files: FileDiffSection[];
  additions: number;
  removals: number;
  /** A lone replace-all edit is diffed once, so its counts are for each match it replaced. */
  countsArePerMatch: boolean;
  /**
   * The counts don't measure the change: a batch's replace-all edit counts one match, and a
   * change too large to compare is a whole-block replacement, which can overstate it.
   */
  countsAreInexact: boolean;
}

interface EditPair {
  oldText: string;
  newText: string;
  replaceAll: boolean;
}

interface DiffLine {
  /** `gap` stands for unchanged lines left out between two changes. */
  kind: "context" | "added" | "removed" | "gap";
  text: string;
}

const GAP: DiffLine = { kind: "gap", text: "" };

/** Unchanged lines kept around each change. */
const CONTEXT_LINES = 2;
// Line-by-line comparison costs old × new steps, shared by all of a call's
// edits; a changed region past the remaining budget is a whole-block replacement.
const MAX_LINE_DIFF_CELLS = 250_000;

/** Claude `Edit`/`MultiEdit`, Omnigent `sys_os_edit`, Pi and OpenCode `edit`. */
const EDIT_PAIR_TOOLS = new Set(["Edit", "MultiEdit", "sys_os_edit", "edit"]);

/** Claude `Write`, Omnigent `sys_os_write`, Pi and OpenCode `write`. */
const WRITE_TOOLS = new Set(["Write", "sys_os_write", "write"]);

// Outputs that say a write created its file: Claude, and OpenCode's newer write
// tool. Pi's and OpenCode's released writes do not say.
const WRITE_CREATED_RE = /^(?:File created successfully at|Created file successfully): /;

/** Tool names whose calls `getFileEditDiff` understands. */
export const FILE_EDIT_TOOL_NAMES: readonly string[] = [
  ...EDIT_PAIR_TOOLS,
  "apply_patch",
  ...WRITE_TOOLS,
];

/**
 * The diff an edit call describes, or `null` when its arguments carry no change.
 * A batch's counts add up each edit's own change, which can exceed the file's net change.
 * A write is all added lines once its `output` says it created the file, and a
 * `write` section otherwise, since what it replaced is not recorded.
 */
export function getFileEditDiff(
  name: string,
  args: Record<string, unknown>,
  output: string | null = null,
): FileEditDiff | null {
  const tool = stripOmnigentPrefix(name);
  if (EDIT_PAIR_TOOLS.has(tool)) return pairsDiff(editPairs(args), editPath(args));
  if (tool === "apply_patch") return patchChangesDiff(args.changes);
  if (WRITE_TOOLS.has(tool)) {
    return writeDiff(args, output !== null && writeCreatedFile(tool, output));
  }
  return null;
}

/** Whether a write's output says the file did not exist before. */
function writeCreatedFile(tool: string, output: string): boolean {
  const parsed = parseToolOutput(output);
  if (tool === "sys_os_write") {
    return parsed.kind === "json" && isRecord(parsed.value) && parsed.value.created === true;
  }
  return WRITE_CREATED_RE.test(firstLine(parsed.text));
}

function writeDiff(args: Record<string, unknown>, created: boolean): FileEditDiff | null {
  if (typeof args.content !== "string") return null;
  const lines = splitLines(args.content);
  const file: FileDiffSection = created
    ? {
        path: editPath(args),
        kind: "add",
        text: lines.map((line) => `+${line}`).join("\n"),
        additions: lines.length,
        removals: 0,
      }
    : { path: editPath(args), kind: "write", text: lines.join("\n"), additions: 0, removals: 0 };
  return {
    files: [file],
    additions: file.additions,
    removals: 0,
    countsArePerMatch: false,
    countsAreInexact: false,
  };
}

/** Read one old/new pair, accepting each harness's key style. */
function asPair(value: unknown): EditPair | null {
  if (!isRecord(value)) return null;
  const oldText = value.old_string ?? value.oldText ?? value.oldString;
  const newText = value.new_string ?? value.newText ?? value.newString;
  if (typeof oldText !== "string" || typeof newText !== "string") return null;
  if (oldText === newText) return null;
  const replaceAll = value.replace_all === true || value.replaceAll === true;
  return { oldText, newText, replaceAll };
}

/** The edited file under each harness's key, or "" when the call names none. */
function editPath(args: Record<string, unknown>): string {
  const path = args.file_path ?? args.path ?? args.filePath;
  return typeof path === "string" ? path : "";
}

function editPairs(args: Record<string, unknown>): EditPair[] {
  if (Array.isArray(args.edits)) {
    return args.edits.map(asPair).filter((pair): pair is EditPair => pair !== null);
  }
  const single = asPair(args);
  return single === null ? [] : [single];
}

function pairsDiff(pairs: EditPair[], path: string): FileEditDiff | null {
  const budget: DiffBudget = { cells: MAX_LINE_DIFF_CELLS, exceeded: false };
  const hunks = pairs.map((pair) => lineDiff(...pairLines(pair), budget));
  const changed = hunks.filter((hunk) => hunk.some((line) => line.kind !== "context"));
  if (changed.length === 0) return null;
  const lines = changed.flatMap((hunk) => (changed.length > 1 ? [GAP, ...hunk] : hunk));
  const additions = countKind(lines, "added");
  const removals = countKind(lines, "removed");
  const text = lines.map(formatLine).join("\n");
  const perMatch = pairs.length === 1 && pairs[0]?.replaceAll === true;
  return {
    files: [{ path, kind: "update", text, additions, removals }],
    additions,
    removals,
    countsArePerMatch: perMatch,
    countsAreInexact: budget.exceeded || (!perMatch && pairs.some((pair) => pair.replaceAll)),
  };
}

/**
 * Codex `fileChange` entries: `{path, kind: {type, move_path?}, diff}`, where
 * `diff` is the new file's content for `add`, the old content for `delete`, and
 * a unified diff for `update`; `move_path` names a rename's destination.
 */
function patchChangesDiff(changes: unknown): FileEditDiff | null {
  if (!Array.isArray(changes)) return null;
  const files: FileDiffSection[] = [];
  for (const change of changes) {
    if (!isRecord(change) || typeof change.path !== "string") continue;
    const kind = isRecord(change.kind) ? change.kind.type : change.kind;
    const movePath = isRecord(change.kind) ? (change.kind.move_path ?? change.kind.movePath) : null;
    const newPath = typeof movePath === "string" && movePath !== "" ? movePath : change.path;
    const diff = typeof change.diff === "string" ? change.diff : "";
    const moved = newPath === change.path ? {} : { newPath };
    if (kind === "add" || kind === "delete") {
      const prefix = kind === "add" ? "+" : "-";
      const body = splitLines(diff).map((line) => `${prefix}${line}`);
      const count = body.length;
      files.push({
        path: change.path,
        ...moved,
        kind,
        text: body.join("\n"),
        additions: kind === "add" ? count : 0,
        removals: kind === "delete" ? count : 0,
      });
      continue;
    }
    const body = unifiedDiffBody(diff);
    const additions = body.filter((line) => line.startsWith("+")).length;
    const removals = body.filter((line) => line.startsWith("-")).length;
    // An empty add or delete still changes a file; an unchanged, unrenamed update does not.
    if (additions + removals === 0 && newPath === change.path) continue;
    files.push({
      path: change.path,
      ...moved,
      kind: "update",
      text: body.join("\n"),
      additions,
      removals,
    });
  }
  if (files.length === 0) return null;
  return {
    files,
    additions: files.reduce((sum, file) => sum + file.additions, 0),
    removals: files.reduce((sum, file) => sum + file.removals, 0),
    countsArePerMatch: false,
    countsAreInexact: false,
  };
}

/** A unified diff's hunk lines, without any `---`/`+++` file headers before the first hunk. */
function unifiedDiffBody(diff: string): string[] {
  const lines = splitLines(diff);
  const firstHunk = lines.findIndex((line) => line.startsWith("@@"));
  if (firstHunk !== -1) return lines.slice(firstHunk);
  // Without a hunk header, file headers are one leading "---"/"+++" pair; a later
  // "--- x" is a removed "-- x" line.
  const hasHeaders = lines[0]?.startsWith("--- ") === true && lines[1]?.startsWith("+++ ") === true;
  return lines.slice(hasHeaders ? 2 : 0);
}

function splitLines(text: string): string[] {
  if (text === "") return [];
  const lines = text.split("\n");
  // A trailing newline ends the last line rather than starting another.
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

function pairLines(pair: EditPair): [string[], string[]] {
  const oldLines = splitLines(pair.oldText);
  const newLines = splitLines(pair.newText);
  // Only a final newline changed: show it as an added or removed blank line.
  if (oldLines.length === newLines.length && oldLines.every((line, i) => line === newLines[i])) {
    (pair.oldText.endsWith("\n") ? oldLines : newLines).push("");
  }
  return [oldLines, newLines];
}

/** Comparison steps a call's remaining edits may still use, and whether one ran out. */
interface DiffBudget {
  cells: number;
  exceeded: boolean;
}

/** Line diff of a replacement, trimmed to the changes and their nearby context. */
function lineDiff(oldLines: string[], newLines: string[], budget: DiffBudget): DiffLine[] {
  let start = 0;
  while (
    start < oldLines.length &&
    start < newLines.length &&
    oldLines[start] === newLines[start]
  ) {
    start += 1;
  }
  let oldEnd = oldLines.length;
  let newEnd = newLines.length;
  while (oldEnd > start && newEnd > start && oldLines[oldEnd - 1] === newLines[newEnd - 1]) {
    oldEnd -= 1;
    newEnd -= 1;
  }
  const middle = middleDiff(oldLines.slice(start, oldEnd), newLines.slice(start, newEnd), budget);
  const context = (lines: string[]): DiffLine[] => lines.map((text) => ({ kind: "context", text }));
  return [
    ...context(oldLines.slice(Math.max(0, start - CONTEXT_LINES), start)),
    ...trimInnerContext(middle),
    ...context(oldLines.slice(oldEnd, oldEnd + CONTEXT_LINES)),
  ];
}

/** Longest-common-subsequence diff of the changed region. */
function middleDiff(oldLines: string[], newLines: string[], budget: DiffBudget): DiffLine[] {
  const replaceAll = (): DiffLine[] => [
    ...oldLines.map((text): DiffLine => ({ kind: "removed", text })),
    ...newLines.map((text): DiffLine => ({ kind: "added", text })),
  ];
  const cells = oldLines.length * newLines.length;
  if (cells > budget.cells) {
    budget.exceeded = true;
    return replaceAll();
  }
  budget.cells -= cells;
  const width = newLines.length + 1;
  // common[i * width + j]: LCS length of oldLines[i:] and newLines[j:].
  const common = new Uint32Array((oldLines.length + 1) * width);
  for (let i = oldLines.length - 1; i >= 0; i -= 1) {
    for (let j = newLines.length - 1; j >= 0; j -= 1) {
      common[i * width + j] =
        oldLines[i] === newLines[j]
          ? common[(i + 1) * width + j + 1]! + 1
          : Math.max(common[(i + 1) * width + j]!, common[i * width + j + 1]!);
    }
  }
  const lines: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < oldLines.length || j < newLines.length) {
    if (i < oldLines.length && j < newLines.length && oldLines[i] === newLines[j]) {
      lines.push({ kind: "context", text: oldLines[i]! });
      i += 1;
      j += 1;
    } else if (
      j >= newLines.length ||
      (i < oldLines.length && common[(i + 1) * width + j]! >= common[i * width + j + 1]!)
    ) {
      lines.push({ kind: "removed", text: oldLines[i]! });
      i += 1;
    } else {
      lines.push({ kind: "added", text: newLines[j]! });
      j += 1;
    }
  }
  return lines;
}

/** Collapse long unchanged runs between two changes to their nearby context. */
function trimInnerContext(lines: DiffLine[]): DiffLine[] {
  const result: DiffLine[] = [];
  let run: DiffLine[] = [];
  for (const line of lines) {
    if (line.kind === "context") {
      run.push(line);
      continue;
    }
    if (run.length > CONTEXT_LINES * 2 + 1) {
      result.push(...run.slice(0, CONTEXT_LINES), GAP, ...run.slice(-CONTEXT_LINES));
    } else {
      result.push(...run);
    }
    run = [];
    result.push(line);
  }
  return [...result, ...run];
}

function formatLine(line: DiffLine): string {
  if (line.kind === "added") return `+${line.text}`;
  if (line.kind === "removed") return `-${line.text}`;
  return line.kind === "gap" ? "@@" : ` ${line.text}`;
}

function countKind(lines: DiffLine[], kind: DiffLine["kind"]): number {
  return lines.filter((line) => line.kind === kind).length;
}
