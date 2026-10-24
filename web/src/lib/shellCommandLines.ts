// Finds where each command of a multi-line shell script starts, so the chat can
// prompt every command and fold the script onto one line. A display aid that
// tracks quotes, heredocs, blocks and line continuations, not a full parser.

type Quote = "'" | '"' | "`" | "$'";
type Construct = "paren" | "case" | "block";

interface ScanState {
  quote: Quote | null;
  /** Open `(`, `case` and `if`/`for`/`while`/`{` constructs. */
  stack: Construct[];
  /** Heredocs whose bodies start on the next line, in order. */
  heredocs: { delimiter: string; stripTabs: boolean }[];
  /** The previous line ended mid-command (`\` or a trailing `&&`, `||`, `|`). */
  continues: boolean;
  /** The next word is a command name, so it may be a keyword. */
  commandPosition: boolean;
}

const BLOCK_OPENERS = new Set(["if", "for", "while", "until", "select"]);
const BLOCK_CLOSERS = new Set(["fi", "done"]);
// Words after these are names or values, not commands (`for name in ...`).
const TAKES_NAME = new Set(["for", "select"]);
// Keywords followed by another command.
const KEEPS_COMMAND_POSITION = new Set(["then", "do", "else", "elif", "!", "time"]);
const CONTINUING_OPERATORS = new Set(["&&", "||", "|", "|&"]);
const OPERATOR_RE = /;;&|;;|;&|&&|\|\||\|&|<<<|<<-|<<|&>>|&>|>>|>&|<&|>\||<>|[;&|()<>]/y;
const HEREDOC_DELIMITER_RE = /^[ \t]*((?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"[^"]*")+)/;

/**
 * For each line of `script` (split on `\n`), whether a new command starts there.
 * Blank lines and lines that continue a command (inside quotes, a heredoc, a
 * block, or after `\`) report `false`.
 */
export function shellCommandStarts(script: string): boolean[] {
  const state: ScanState = {
    quote: null,
    stack: [],
    heredocs: [],
    continues: false,
    commandPosition: true,
  };
  return script.split("\n").map((text) => {
    const line = text.endsWith("\r") ? text.slice(0, -1) : text;
    const heredoc = state.heredocs[0];
    if (heredoc !== undefined) {
      const body = heredoc.stripTabs ? line.replace(/^\t+/, "") : line;
      if (body === heredoc.delimiter) state.heredocs.shift();
      return false;
    }
    const startsCommand =
      state.quote === null && state.stack.length === 0 && !state.continues && line.trim() !== "";
    scanLine(line, state);
    return startsCommand;
  });
}

/**
 * The script on one line for a collapsed row: its commands joined with `; `,
 * continuation lines with a space, and comment-only lines left out.
 */
export function oneLineShellCommand(script: string): string {
  const starts = shellCommandStarts(script);
  const commands: string[] = [];
  script.split("\n").forEach((text, index) => {
    const line = text.trim();
    if (line === "") return;
    if (starts[index]) {
      if (!line.startsWith("#")) commands.push(line);
    } else if (commands.length > 0) {
      const last = commands.length - 1;
      commands[last] = `${commands[last].replace(/\\$/, "").trimEnd()} ${line}`;
    } else {
      commands.push(line);
    }
  });
  if (commands.length === 0) return script.trim();
  return commands.reduce((joined, command) =>
    // A newline after `;` or `&` already ends the command, so `; ` would double it.
    /[;&]$/.test(joined) ? `${joined} ${command}` : `${joined}; ${command}`,
  );
}

function scanLine(line: string, state: ScanState): void {
  let index = 0;
  // Unquoted text of the word being read, or null between words.
  let word: string | null = null;
  let plain = true;
  let lastToken: string | null = null;
  let escapedNewline = false;

  const finishWord = () => {
    if (word !== null) {
      if (plain) applyWord(word, state);
      else state.commandPosition = false;
      lastToken = "word";
    }
    word = null;
    plain = true;
  };

  while (index < line.length) {
    if (state.quote !== null) {
      index = skipQuoted(line, index, state);
      continue;
    }
    const char = line[index];
    if (char === " " || char === "\t") {
      finishWord();
      index += 1;
    } else if (char === "#" && word === null) {
      break;
    } else if (char === "\\") {
      if (index === line.length - 1) {
        escapedNewline = true;
        break;
      }
      word = (word ?? "") + line[index + 1];
      plain = false;
      index += 2;
    } else if (char === "'" || char === '"' || char === "`") {
      state.quote = char;
      word ??= "";
      plain = false;
      index += 1;
    } else if (char === "$" && line[index + 1] === "'") {
      state.quote = "$'";
      word ??= "";
      plain = false;
      index += 2;
    } else if (char === "$" && line[index + 1] === "(") {
      plain = false;
      finishWord();
      state.stack.push("paren");
      state.commandPosition = true;
      lastToken = "(";
      index += 2;
    } else if (!";&|()<>".includes(char)) {
      word = (word ?? "") + char;
      index += 1;
    } else {
      OPERATOR_RE.lastIndex = index;
      const operator = OPERATOR_RE.exec(line)![0];
      finishWord();
      lastToken = operator;
      index += operator.length;
      if (operator === "<<" || operator === "<<-") {
        index = readHeredocDelimiter(line, index, operator === "<<-", state);
      } else if (operator === "(") {
        state.stack.push("paren");
        state.commandPosition = true;
      } else if (operator === ")") {
        // Inside `case`, `pattern)` closes nothing.
        if (state.stack.at(-1) === "paren") state.stack.pop();
        state.commandPosition = true;
      } else if (!/^(?:[<>]|&>)/.test(operator)) {
        state.commandPosition = true;
      }
    }
  }
  finishWord();
  if (state.quote !== null) return;
  state.continues = escapedNewline || (lastToken !== null && CONTINUING_OPERATORS.has(lastToken));
  if (!escapedNewline) state.commandPosition = true;
}

function applyWord(word: string, state: ScanState): void {
  // A bare `{` or `}` is taken as a group even off command position, which
  // also covers `function name {`.
  if (word === "{") {
    state.stack.push("block");
    state.commandPosition = true;
    return;
  }
  if (word === "}") {
    if (state.stack.at(-1) === "block") state.stack.pop();
    state.commandPosition = false;
    return;
  }
  if (!state.commandPosition) return;
  if (word === "case") {
    state.stack.push("case");
    state.commandPosition = false;
  } else if (word === "esac") {
    const caseIndex = state.stack.lastIndexOf("case");
    if (caseIndex >= 0) state.stack.length = caseIndex;
    state.commandPosition = false;
  } else if (BLOCK_OPENERS.has(word)) {
    state.stack.push("block");
    state.commandPosition = !TAKES_NAME.has(word);
  } else if (BLOCK_CLOSERS.has(word)) {
    if (state.stack.at(-1) === "block") state.stack.pop();
    state.commandPosition = false;
  } else if (!KEEPS_COMMAND_POSITION.has(word)) {
    state.commandPosition = false;
  }
}

/** Advance past quoted text, closing the quote if it ends on this line. */
function skipQuoted(line: string, index: number, state: ScanState): number {
  const closer = state.quote === "$'" ? "'" : state.quote;
  for (let at = index; at < line.length; at += 1) {
    // Only single quotes take backslashes literally.
    if (line[at] === "\\" && state.quote !== "'") {
      at += 1;
    } else if (line[at] === closer) {
      state.quote = null;
      return at + 1;
    }
  }
  return line.length;
}

function readHeredocDelimiter(
  line: string,
  index: number,
  stripTabs: boolean,
  state: ScanState,
): number {
  const match = HEREDOC_DELIMITER_RE.exec(line.slice(index));
  if (match === null) return index;
  state.heredocs.push({ delimiter: match[1].replace(/\\(.)|['"]/g, "$1"), stripTabs });
  return index + match[0].length;
}
