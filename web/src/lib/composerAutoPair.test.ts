import { describe, expect, it } from "vitest";
import { applyEdit, type ComposerEdit } from "./composerEditing";
import {
  NO_PAIRS,
  editAfterKeystroke,
  editBeforeKeystroke,
  followKeystroke,
  isPairChar,
  pairsAround,
  type PairState,
} from "./composerAutoPair";

/**
 * A draft typed into the way the composer handles keystrokes: the same three
 * steps its input hook takes (before a keystroke, the keystroke landing, the
 * edit after it), with an undo history of one step per keystroke and per edit,
 * as the textarea keeps. `|` marks the caret; `[` and `]` a selection.
 */
class Draft {
  text: string;
  start: number;
  end: number;
  state: PairState = NO_PAIRS;
  history: [string, number, number][] = [];

  constructor(marked = "|") {
    const open = marked.indexOf("[");
    if (open !== -1) {
      const close = marked.indexOf("]");
      this.text = marked.slice(0, open) + marked.slice(open + 1, close) + marked.slice(close + 1);
      this.start = open;
      this.end = close - 1;
    } else {
      const caret = marked.indexOf("|");
      this.text = marked.slice(0, caret) + marked.slice(caret + 1);
      this.start = caret;
      this.end = this.start;
    }
  }

  private save() {
    this.history.push([this.text, this.start, this.end]);
  }

  private apply(edit: ComposerEdit) {
    this.save();
    this.text = applyEdit(this.text, edit);
    this.start = edit.caret;
    this.end = this.start;
  }

  /** Type each character (or "Backspace") as one keystroke. */
  type(...keys: string[]) {
    for (const key of keys.flatMap((k) => (k === "Backspace" ? [k] : [...k]))) this.key(key);
    return this;
  }

  private key(key: string) {
    const inputType = key === "Backspace" ? "deleteContentBackward" : "insertText";
    const data = key === "Backspace" ? null : key;
    const replaced = editBeforeKeystroke(
      this.text,
      this.start,
      this.end,
      inputType,
      data,
      this.state,
    );
    if (replaced) {
      if (replaced.edit) this.apply(replaced.edit);
      if (replaced.selection) [this.start, this.end] = replaced.selection;
      this.state = replaced.state;
      return;
    }
    const before = { value: this.text, start: this.start, end: this.end };
    this.save();
    if (key === "Backspace") {
      const from = this.start === this.end ? Math.max(0, this.start - 1) : this.start;
      this.text = this.text.slice(0, from) + this.text.slice(this.end);
      this.start = from;
      this.end = this.start;
    } else {
      this.text = this.text.slice(0, this.start) + key + this.text.slice(this.end);
      this.start = this.start + 1;
      this.end = this.start;
    }
    this.state = followKeystroke(this.state, inputType, before, this.text, this.start);
    if (inputType !== "insertText" || (!isPairChar(key) && key !== " ")) {
      this.state = { ...this.state, swallow: null };
      return;
    }
    const result = editAfterKeystroke(this.text, this.start, key, this.state);
    this.state = result.state;
    if (result.edit) this.apply(result.edit);
  }

  /** Move the caret (a click or arrow key): a closer left behind stops being tracked. */
  moveTo(caret: number) {
    this.start = caret;
    this.end = this.start;
    this.state = {
      pairs: pairsAround(this.state.pairs, caret),
      swallow: this.state.swallow?.at === caret ? this.state.swallow : null,
    };
    return this;
  }

  undo() {
    const [text, start, end] = this.history.pop()!;
    this.text = text;
    this.start = start;
    this.end = end;
    // A history step isn't typed input: tracking ends.
    this.state = NO_PAIRS;
    return this;
  }

  toString() {
    if (this.start === this.end)
      return this.text.slice(0, this.start) + "|" + this.text.slice(this.start);
    return `${this.text.slice(0, this.start)}[${this.text.slice(this.start, this.end)}]${this.text.slice(this.end)}`;
  }
}

const typed = (...keys: string[]) => String(new Draft().type(...keys));
const typedInto = (marked: string, ...keys: string[]) => String(new Draft(marked).type(...keys));

describe("auto-pairing inline markers", () => {
  it("inserts the closer after an opener", () => {
    expect(typed("*")).toBe("*|*");
    expect(typed("_")).toBe("_|_");
    expect(typed("say `")).toBe("say `|`");
    expect(typed("say *")).toBe("say *|*");
    expect(typed("(*")).toBe("(*|*");
    expect(typedInto("say |.", "_")).toBe("say _|_.");
    expect(typedInto("> |", "*")).toBe("> *|*");
  });

  it("grows an empty pair: bold, bold italic, double-backtick code", () => {
    expect(typed("**")).toBe("**|**");
    expect(typed("***")).toBe("***|***");
    expect(typed("__")).toBe("__|__");
    expect(typed("run ``")).toBe("run ``|``");
  });

  it("styles the span as it's typed: it's closed all along", () => {
    expect(typed("**bold")).toBe("**bold|**");
    expect(typed("say `code")).toBe("say `code|`");
  });

  it("steps over the inserted closer, one * over a whole **", () => {
    expect(typed("*hi*")).toBe("*hi*|");
    expect(typed("**bold*")).toBe("**bold**|");
    expect(typed("__bold_")).toBe("__bold__|");
    expect(typed("say `code`")).toBe("say `code`|");
    expect(typed("run ``a`")).toBe("run ``a``|");
    expect(typed("**bold* after")).toBe("**bold** after|");
  });

  it("drops the rest of a closer typed in full, so it isn't doubled", () => {
    expect(typed("**bold**")).toBe("**bold**|");
    expect(typed("***both***")).toBe("***both***|");
    expect(typed("run ``a``")).toBe("run ``a``|");
    expect(typed("__bold__ x")).toBe("__bold__ x|");
    // Only right there: after moving on, a `*` is typed as usual.
    expect(typed("**bold* *")).toBe("**bold** *|*");
  });

  it("tidies a stray space as it steps over", () => {
    expect(typed("**bold *")).toBe("**bold**|");
  });

  it("closes an open run instead of pairing after it", () => {
    // The `**` whose closer a space removed is still open: these close it.
    expect(typed("say ** both sides **")).toBe("say **both sides**|");
    expect(typed("say ** both sides *")).toBe("say **both sides**|");
    expect(typedInto("say `code|", "`")).toBe("say `code`|");
    // Runs that can't open (bullets, maths, intraword) don't stop pairing.
    expect(typed("2 * 3 *")).toBe("2 * 3 *|*");
    expect(typed("snake_case _")).toBe("snake_case _|_");
  });

  it("only pairs where the marker opens a span", () => {
    // Intraword, after text, before a word, escaped.
    expect(typed("snake_")).toBe("snake_|");
    expect(typed("snake_case")).toBe("snake_case|");
    expect(typedInto("|word", "*")).toBe("*|word");
    expect(typed("\\*")).toBe("\\*|");
    expect(typed("a*")).toBe("a*|");
  });

  it("never types over what the user typed", () => {
    expect(typedInto("*hi|*", "*")).toBe("*hi*|*");
    // A closer left behind by a caret move is plain text from then on.
    const draft = new Draft().type("*hi");
    draft.moveTo(draft.text.length).moveTo(3).type("*");
    expect(String(draft)).toBe("*hi*|*");
  });

  it("lets a space after an empty opener mean a bullet or maths", () => {
    expect(typed("* item")).toBe("* item|");
    expect(typedInto("> |", "* item")).toBe("> * item|");
    expect(typed("2 * 3")).toBe("2 * 3|");
    expect(typed("2 ** 3")).toBe("2 ** 3|");
    expect(typed("_ x")).toBe("_ x|");
    // A code span may start with a space: its closer stays.
    expect(typed("run ` x")).toBe("run ` x|`");
  });

  it("deletes both markers with Backspace in an empty pair", () => {
    expect(typed("*", "Backspace")).toBe("|");
    expect(typed("**", "Backspace")).toBe("*|*");
    expect(typed("**", "Backspace", "Backspace")).toBe("|");
    expect(typed("say `", "Backspace")).toBe("say |");
    // With text inside, Backspace deletes a character as usual.
    expect(typed("*a", "Backspace")).toBe("*|*");
    expect(typed("*a", "Backspace", "Backspace")).toBe("|");
  });

  it("undoes an insertion or a step-over in one step, back to what was typed", () => {
    expect(String(new Draft().type("*").undo())).toBe("*|");
    expect(String(new Draft().type("**").undo())).toBe("**|*");
    expect(String(new Draft().type("*hi*").undo())).toBe("*hi*|*");
    expect(String(new Draft().type("* ").undo())).toBe("* |*");
    // A closer's rest typed in full does nothing, so it leaves no step to undo.
    expect(String(new Draft().type("**bold**").undo())).toBe("**bold*|**");
    expect(String(new Draft("**text|").type("**").undo())).toBe("**text*|");
  });

  it("leaves code and fences alone", () => {
    expect(typedInto("`co|de`", "*")).toBe("`co*|de`");
    expect(typedInto("say `x |`", "_")).toBe("say `x _|`");
    expect(typedInto("```\n|\n```", "*")).toBe("```\n*|\n```");
    expect(typedInto("```\nx |\n```", "`")).toBe("```\nx `|\n```");
  });

  it("still turns ``` on an empty line into a code block", () => {
    expect(typed("```")).toBe("```\n|\n```");
    expect(typedInto("intro\n|", "```")).toBe("intro\n```\n|\n```");
    expect(typedInto("> |", "```")).toBe("> ```\n> |\n> ```");
    expect(typedInto("- |", "```")).toBe("- ```\n  |\n  ```");
  });
});

describe("line breaks", () => {
  it("end tracking, also when inserted as text (Alt+Enter)", () => {
    const draft = new Draft().type("**bold");
    expect(draft.state.pairs).toHaveLength(1);
    const before = { value: draft.text, start: draft.start, end: draft.end };
    const after = `${draft.text.slice(0, draft.start)}\n${draft.text.slice(draft.end)}`;
    for (const inputType of ["insertText", "insertLineBreak"])
      expect(followKeystroke(draft.state, inputType, before, after, draft.start + 1)).toEqual(
        NO_PAIRS,
      );
  });
});

describe("completing an open ** typed by hand", () => {
  it("makes `**text*` bold, and drops a second * typed right after", () => {
    expect(typedInto("**text|", "*")).toBe("**text**|");
    expect(typedInto("**text|", "**")).toBe("**text**|");
    expect(typedInto("**text|", "* and")).toBe("**text** and|");
    expect(typedInto("**text|", "***")).toBe("**text***|");
    expect(typedInto("say **it|", "*.")).toBe("say **it**.|");
  });

  it("trims stray spaces as it completes", () => {
    expect(typedInto("** Is this Bold?!|", "*")).toBe("**Is this Bold?!**|");
    expect(typedInto("**bold |", "*")).toBe("**bold**|");
  });

  it("leaves real italics, arithmetic, code and escapes alone", () => {
    expect(typedInto("*it|", "*")).toBe("*it*|");
    expect(typedInto("**a** b|", "*")).toBe("**a** b*|");
    expect(typedInto("2 **3|", "*")).toBe("2 **3*|");
    expect(typedInto("`**code|`", "*")).toBe("`**code*|`");
    expect(typedInto("**text\\|", "*")).toBe("**text\\*|");
    expect(typedInto("**a\n\nb|", "*")).toBe("**a\n\nb*|");
  });
});

describe("typing a marker over a selection", () => {
  it("wraps a simple selection, and again for bold", () => {
    expect(typedInto("say [word] now", "*")).toBe("say *[word]* now");
    expect(typedInto("say [word] now", "**")).toBe("say **[word]** now");
    expect(typedInto("say [code] now", "`")).toBe("say `[code]` now");
    expect(typedInto("say [word] now", "_")).toBe("say _[word]_ now");
  });

  it("replaces anything else, as typing always did", () => {
    expect(typedInto("sn[ake]", "_")).toBe("sn_|");
    expect(typedInto("[two\nlines]", "*")).toBe("*|*");
    expect(typedInto("say[ word]", "*")).toBe("say*|");
    expect(typedInto("`c[od]e`", "*")).toBe("`c*|e`");
  });
});
