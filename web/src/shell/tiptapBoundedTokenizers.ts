import { TaskList } from "@tiptap/extension-list";
import { Table } from "@tiptap/extension-table";

function lineEnd(source: string, start: number): number {
  const end = source.indexOf("\n", start);
  return end === -1 ? source.length : end;
}

const TASK_ITEM = /^\s*[-+*]\s+\[[ xX]\]\s/;

// Keep the prefix that parseIndentedBlocks can inspect, including blank-line
// lookahead and nested content. Upstream still constructs every token.
function taskListPrefix(source: string): string | null {
  let start = 0;
  let end = lineEnd(source, start);
  while (source.slice(start, end).trim() === "") {
    if (end === source.length) return null;
    start = end + 1;
    end = lineEnd(source, start);
  }

  const first = source.slice(start, end);
  if (!TASK_ITEM.test(first)) return null;
  let itemIndent = first.length - first.trimStart().length;
  let blankStart: number | null = null;

  while (end < source.length) {
    start = end + 1;
    end = lineEnd(source, start);
    const line = source.slice(start, end);
    if (line.trim() === "") {
      blankStart ??= start;
      continue;
    }
    const indent = line.length - line.trimStart().length;
    // A blank ends the upstream list unless deeper content follows it.
    if (blankStart !== null && indent <= itemIndent) {
      return source.slice(0, blankStart - 1);
    }
    blankStart = null;
    if (TASK_ITEM.test(line)) {
      itemIndent = Math.min(itemIndent, indent);
    } else if (indent <= itemIndent) {
      return source.slice(0, start - 1);
    }
  }
  return source;
}

// TODO: Remove these bounds when upstream stops splitting the whole remaining
// document in task-list tokenization and table-start detection.
const taskTokenizer = TaskList.config.markdownTokenizer!;

export const BoundedTaskList = TaskList.extend({
  markdownTokenizer: {
    ...taskTokenizer,
    tokenize(source, tokens, lexer) {
      const prefix = taskListPrefix(source);
      return prefix === null ? undefined : taskTokenizer.tokenize(prefix, tokens, lexer);
    },
  },
});

const tableTokenizer = Table.config.markdownTokenizer!;
const tableStart = tableTokenizer.start;

// The upstream start callback only uses the header and delimiter lines.
const boundedTableStart =
  typeof tableStart === "function"
    ? (source: string) => tableStart(source.slice(0, lineEnd(source, lineEnd(source, 0) + 1)))
    : tableStart;

/** Whether *source* opens with a table header and delimiter row. */
export function startsTable(source: string): boolean {
  return typeof boundedTableStart === "function" && boundedTableStart(source) === 0;
}

export const BoundedTable = Table.extend({
  markdownTokenizer: {
    ...tableTokenizer,
    start: boundedTableStart,
  },
});
