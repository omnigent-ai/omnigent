// Finds where each command of a multi-line shell script starts, so the chat can
// prompt every command and fold the script onto one line. A display aid that
// tracks quotes, heredocs, blocks and line continuations, not a full parser.

type Quote = "'" | '"' | "`" | "$'";
type Construct = "paren" | "arith" | "case" | "block";

interface ScanState {
  quote: Quote | null;
  /** Open `(`, `((`, `case` and `if`/`for`/`while`/`{` constructs. */
  stack: Construct[];
  /** Heredocs whose bodies start on the next line, in order. */
  heredocs: { delimiter: string; stripTabs: boolean }[];
  /** The previous line ended mid-command (`\` or a trailing `&&`, `||`, `|`). */
  continues: boolean;
  /** The next word is a command name, so it may be a keyword. */
  commandPosition: boolean;
  /** A `case` still waits for its `in`. */
  caseHead: boolean;
}

interface ScannedLine {
  /** A new command starts on this line. */
  startsCommand: boolean;
  /** The line without a trailing comment or `\`. */
  code: string;
  /** What joins the next line to this one on a single line. */
  joinNext: "; " | " ";
}

const BLOCK_OPENERS = new Set(["if", "for", "while", "until", "select"]);
const BLOCK_CLOSERS = new Set(["fi", "done"]);
// Words after these are names or values, not commands (`for name in ...`).
const TAKES_NAME = new Set(["for", "select"]);
// Keywords followed by another command.
const KEEPS_COMMAND_POSITION = new Set(["then", "do", "else", "elif", "!", "time"]);
const CONTINUING_OPERATORS = new Set(["&&", "||", "|", "|&"]);
const TERMINATORS = new Set([";", ";;", ";&", ";;&", "&"]);
const OPERATOR_RE = /;;&|;;|;&|&&|\|\||\|&|<<<|<<-|<<|&>>|&>|>>|>&|<&|>\||<>|[;&|()<>]/y;
const HEREDOC_DELIMITER_RE = /^[ \t]*((?:[^\s;&|()<>'"\\]|\\.|'[^']*'|"[^"]*")+)/;

/**
 * For each line of `script` (split on `\n`), whether a new command starts there.
 * Blank lines and lines that continue a command (inside quotes, a heredoc, a
 * block, or after `\`) report `false`.
 */
export function shellCommandStarts(script: string): boolean[] {
  return scanShellScript(script).map((line) => line.startsCommand);
}

/**
 * The script on one line for a collapsed row: a newline that ends a command
 * becomes `; `, other line breaks a space, and comments are left out.
 */
export function oneLineShellCommand(script: string): string {
  let folded = "";
  let separator = "";
  for (const { code, joinNext } of scanShellScript(script)) {
    const text = code.trim();
    if (text === "") continue;
    folded += separator + text;
    separator = joinNext;
  }
  return folded === "" ? script.trim() : folded;
}

function scanShellScript(script: string): ScannedLine[] {
  const state: ScanState = {
    quote: null,
    stack: [],
    heredocs: [],
    continues: false,
    commandPosition: true,
    caseHead: false,
  };
  return script.split("\n").map((text) => {
    const line = text.endsWith("\r") ? text.slice(0, -1) : text;
    const heredoc = state.heredocs[0];
    if (heredoc !== undefined) {
      const body = heredoc.stripTabs ? line.replace(/^\t+/, "") : line;
      const ends = body === heredoc.delimiter;
      if (ends) state.heredocs.shift();
      // The body is data; only the newline after the last terminator ends the command.
      const joinNext = ends && state.heredocs.length === 0 && !state.continues ? "; " : " ";
      return { startsCommand: false, code: line, joinNext };
    }
    const startsCommand =
      state.quote === null && state.stack.length === 0 && !state.continues && line.trim() !== "";
    return { startsCommand, ...scanLine(line, state) };
  });
}

function scanLine(line: string, state: ScanState): Omit<ScannedLine, "startsCommand"> {
  let index = 0;
  // Unquoted text of the word being read, or null between words.
  let word: string | null = null;
  let plain = true;
  let lastToken: string | null = null;
  let escapedNewline = false;
  let commentStart: number | null = null;
  const heredocsBefore = state.heredocs.length;
  // The last token already separates commands or needs more of the command
  // after it (`;`, `&&`, `(`, `then`), so a newline there is not a `;`.
  let joinsWithSpace = false;

  const finishWord = () => {
    if (word !== null) {
      if (plain) {
        joinsWithSpace = applyWord(word, state);
      } else {
        state.commandPosition = false;
        joinsWithSpace = false;
      }
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
      commentStart = index;
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
      const arithmetic = line[index + 2] === "(";
      state.stack.push(arithmetic ? "arith" : "paren");
      state.commandPosition = true;
      lastToken = "(";
      joinsWithSpace = true;
      index += arithmetic ? 3 : 2;
    } else if (!";&|()<>".includes(char)) {
      word = (word ?? "") + char;
      index += 1;
    } else {
      OPERATOR_RE.lastIndex = index;
      const operator = OPERATOR_RE.exec(line)![0];
      finishWord();
      lastToken = operator;
      joinsWithSpace =
        CONTINUING_OPERATORS.has(operator) || TERMINATORS.has(operator) || operator === "(";
      index += operator.length;
      // In arithmetic (`$((...))`, `((...))`) `<<` is a shift, not a heredoc.
      if ((operator === "<<" || operator === "<<-") && !state.stack.includes("arith")) {
        index = readHeredocDelimiter(line, index, operator === "<<-", state);
      } else if (operator === "(") {
        const arithmetic = line[index] === "(";
        state.stack.push(arithmetic ? "arith" : "paren");
        if (arithmetic) index += 1;
        state.commandPosition = true;
      } else if (operator === ")") {
        const open = state.stack.at(-1);
        // `))` closes arithmetic; inside `case`, `pattern)` closes nothing.
        if (open === "arith" && line[index] === ")") index += 1;
        if (open === "arith" || open === "paren") state.stack.pop();
        state.commandPosition = true;
      } else if (!/^(?:[<>]|&>)/.test(operator)) {
        state.commandPosition = true;
      }
    }
  }
  finishWord();
  if (state.quote !== null) return { code: line, joinNext: " " };
  state.continues = escapedNewline || (lastToken !== null && CONTINUING_OPERATORS.has(lastToken));
  if (!escapedNewline) state.commandPosition = true;
  const code = line.slice(0, commentStart ?? (escapedNewline ? -1 : line.length));
  const continued = escapedNewline || joinsWithSpace || state.heredocs.length > heredocsBefore;
  return { code, joinNext: continued ? " " : "; " };
}

/** Apply a word's effect on the open constructs; returns whether a command must follow it. */
function applyWord(word: string, state: ScanState): boolean {
  // A bare `{` or `}` is taken as a group even off command position, which
  // also covers `function name {`.
  if (word === "{") {
    state.stack.push("block");
    state.commandPosition = true;
    return true;
  }
  if (word === "}") {
    if (state.stack.at(-1) === "block") state.stack.pop();
    state.commandPosition = false;
    return false;
  }
  if (word === "in" && state.caseHead) {
    // `case word in`: the patterns follow.
    state.caseHead = false;
    return true;
  }
  if (!state.commandPosition) return false;
  if (word === "case") {
    state.stack.push("case");
    state.caseHead = true;
    state.commandPosition = false;
  } else if (word === "esac") {
    const caseIndex = state.stack.lastIndexOf("case");
    if (caseIndex >= 0) state.stack.length = caseIndex;
    state.commandPosition = false;
  } else if (BLOCK_OPENERS.has(word)) {
    state.stack.push("block");
    state.commandPosition = !TAKES_NAME.has(word);
    return state.commandPosition;
  } else if (BLOCK_CLOSERS.has(word)) {
    if (state.stack.at(-1) === "block") state.stack.pop();
    state.commandPosition = false;
  } else if (KEEPS_COMMAND_POSITION.has(word)) {
    return true;
  } else {
    state.commandPosition = false;
  }
  return false;
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
