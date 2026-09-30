import { describe, expect, it } from "vitest";
import { tokenizeComposerMarkdown } from "./composerMarkdown";
import {
  applyEdit,
  codeFenceAfterTyping,
  fenceArrowDown,
  fenceEnter,
  fenceNewline,
  tidyEmphasisAfterTyping,
  type ComposerEdit,
} from "./composerEditing";

/** `|` marks the caret; returns the text without it and the caret offset. */
function at(marked: string): [string, number] {
  const caret = marked.indexOf("|");
  return [marked.slice(0, caret) + marked.slice(caret + 1), caret];
}

/** The text (with `|` for the caret) after applying an edit, or null. */
function after(marked: string, edit: ComposerEdit | null): string | null {
  if (!edit) return null;
  const [text] = at(marked);
  const result = applyEdit(text, edit);
  return result.slice(0, edit.caret) + "|" + result.slice(edit.caret);
}

const tidy = (marked: string) => after(marked, tidyEmphasisAfterTyping(...at(marked)));

describe("tidyEmphasisAfterTyping", () => {
  it("removes a stray space just inside the opener", () => {
    expect(tidy("** Is this Bold?!**|")).toBe("**Is this Bold?!**|");
    expect(tidy("say __ this__| now")).toBe("say __this__| now");
    expect(tidy("an * aside*|")).toBe("an *aside*|");
    expect(tidy("(_ note_|)")).toBe("(_note_|)");
    expect(tidy("*** both***|")).toBe("***both***|");
  });

  it("removes a stray space just inside the closer", () => {
    expect(tidy("**bold **|")).toBe("**bold**|");
    expect(tidy("x __bold  __|")).toBe("x __bold__|");
    expect(tidy("*it *|")).toBe("*it*|");
    expect(tidy("_it _|")).toBe("_it_|");
  });

  it("trims both inner edges when text remains between them", () => {
    expect(tidy("** both sides **|")).toBe("**both sides**|");
    expect(tidy("say _ both _| now")).toBe("say _both_| now");
    expect(tidy("x *  wide  *|")).toBe("x *wide*|");
  });

  it("leaves list bullets alone, inside quotes and nested lists too", () => {
    for (const bullet of [
      "* item*|",
      "  * item *|",
      "> * item*|",
      "> > * item *|",
      "- * nested*|",
      "1. * nested*|",
      "    * deep item*|",
      ">   - * quoted nested*|",
    ])
      expect(tidy(bullet), bullet).toBeNull();
    // A real opener after a bullet still tidies.
    expect(tidy("> * item with ** bold**|")).toBe("> * item with **bold**|");
  });

  it("leaves arithmetic and intraword underscores alone", () => {
    expect(tidy("2 * 3 *|")).toBeNull();
    expect(tidy("2 ** 3 **|")).toBeNull();
    expect(tidy("2 * 3*|")).toBeNull();
    expect(tidy("2 ** 3**|")).toBeNull();
    expect(tidy("file_ name_|")).toBeNull();
    expect(tidy("snake_case _|")).toBeNull();
  });

  it("leaves code, escapes, blank-line gaps and empty pairs alone", () => {
    expect(tidy("`x ** code**|`")).toBeNull();
    // An unclosed backtick isn't a code span: that pair is real emphasis.
    expect(tidy("`** code**|")).toBe("`**code**|");
    expect(tidy("```\n** in a fence**|")).toBeNull();
    expect(tidy("\\** escaped**|")).toBeNull();
    expect(tidy("** escaped\\**|")).toBeNull();
    expect(tidy("** across\n\na gap**|")).toBeNull();
    expect(tidy("**  **|")).toBeNull();
  });

  it("does nothing for an already-valid pair or a run the caret splits", () => {
    expect(tidy("**fine**|")).toBeNull();
    expect(tidy("** open*|*")).toBeNull();
    expect(tidy("plain text*|")).toBeNull();
  });

  it("only tidies into real emphasis", () => {
    // The tidied `_a_b` would be intraword on its right: not emphasis, no tidy.
    expect(tidy("x _ a_|b")).toBeNull();
  });
});

describe("codeFenceAfterTyping", () => {
  const fence = (marked: string) => after(marked, codeFenceAfterTyping(...at(marked)));

  it("turns ``` typed on an empty line into a block with the caret inside", () => {
    expect(fence("```|")).toBe("```\n|\n```");
    expect(fence("intro\n```|")).toBe("intro\n```\n|\n```");
    expect(fence("intro\n```|\nlater")).toBe("intro\n```\n|\n```\nlater");
  });

  it("leaves other backticks alone", () => {
    expect(fence("x```|")).toBeNull();
    expect(fence("````|")).toBeNull();
    expect(fence("``|")).toBeNull();
    expect(fence("```|ts")).toBeNull();
    // Closing an open block, or opening one an existing fence would close.
    expect(fence("```\ncode\n```|")).toBeNull();
    expect(fence("```|\ncode\n```")).toBeNull();
  });
});

describe("code blocks inside quotes", () => {
  const fence = (marked: string) => after(marked, codeFenceAfterTyping(...at(marked)));
  const enter = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceEnter(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action || action.kind === "newline") return action?.kind ?? null;
    return after(marked, action.edit);
  };
  const down = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceArrowDown(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action) return null;
    if ("move" in action) return text.slice(0, action.move) + "|" + text.slice(action.move);
    return after(marked, action.edit);
  };

  it("creates a quoted block, its lines keeping the quote", () => {
    expect(fence("> ```|")).toBe("> ```\n> |\n> ```");
    expect(fence("> > ```|")).toBe("> > ```\n> > |\n> > ```");
  });

  it("keeps Enter inside the quote, so the block goes on", () => {
    expect(enter("> ```\n> co|de\n> ```")).toBe("> ```\n> co\n> |de\n> ```");
    expect(enter("> ```ts|\n> x\n> ```")).toBe("> ```ts\n> |\n> x\n> ```");
  });

  it("exits a quoted block from its empty last line, staying in the quote", () => {
    expect(enter("> ```\n> code\n> |\n> ```")).toBe("> ```\n> code\n> ```\n> |");
    expect(enter("> ```\n> code\n> |\n> ```\n> more")).toBe("> ```\n> code\n> ```\n|> more");
    // An open quoted block closes on exit.
    expect(enter("> ```\n> code\n> |")).toBe("> ```\n> code\n> ```\n> |");
    expect(enter("> ```\n> code\n> |\nafter")).toBe("> ```\n> code\n> ```\n> |\nafter");
  });

  it("moves past a quoted block with ArrowDown", () => {
    expect(down("> ```\n> co|de\n> ```")).toBe("> ```\n> code\n> ```\n> |");
    expect(down("> ```\n> co|de\n> ```\nafter")).toBe("> ```\n> code\n> ```\n|after");
    expect(down("> ```\n> co|de")).toBe("> ```\n> code\n> ```\n> |");
  });

  it("treats text in a quoted block as code", () => {
    expect(tidy("> ```\n> ** not bold**|")).toBeNull();
  });
});

describe("code blocks inside list items", () => {
  const fence = (marked: string) => after(marked, codeFenceAfterTyping(...at(marked)));
  const enter = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceEnter(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action || action.kind === "newline") return action?.kind ?? null;
    return after(marked, action.edit);
  };
  const down = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceArrowDown(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action) return null;
    if ("move" in action) return text.slice(0, action.move) + "|" + text.slice(action.move);
    return after(marked, action.edit);
  };

  it("creates a block whose lines continue the item", () => {
    expect(fence("- ```|")).toBe("- ```\n  |\n  ```");
    expect(fence("> - ```|")).toBe("> - ```\n>   |\n>   ```");
    expect(fence("1. ```|")).toBe("1. ```\n   |\n   ```");
  });

  it("follows the item an earlier line opens or closes, for the same fence lines", () => {
    // The same three lines, first as a quote's fence, then (a list item added
    // above them) as the item's, then as the quote's again: each parse keeps
    // its own prefix, so Enter indents into the item only while it's there.
    const lines = ">   ```\n>   co|de\n>   ```";
    expect(enter(lines)).toBe(">   ```\n>   co\n> |de\n>   ```");
    expect(enter(`> - item\n${lines}`)).toBe("> - item\n>   ```\n>   co\n>   |de\n>   ```");
    expect(enter(lines)).toBe(">   ```\n>   co\n> |de\n>   ```");
    expect(enter(`> - item\n${lines}`)).toBe("> - item\n>   ```\n>   co\n>   |de\n>   ```");
  });

  it("makes Enter a line of the block, indented into the item", () => {
    expect(enter("> - ```\n>   ** not| bold**\n>   ```")).toBe(
      "> - ```\n>   ** not\n>   | bold**\n>   ```",
    );
    expect(enter("- ```\n  co|de\n  ```")).toBe("- ```\n  co\n  |de\n  ```");
  });

  it("exits from the empty last line, staying in the item", () => {
    expect(enter("> - ```\n>   code\n>   |\n>   ```")).toBe("> - ```\n>   code\n>   ```\n>   |");
    expect(enter("- ```\n  code\n  |")).toBe("- ```\n  code\n  ```\n  |");
  });

  it("moves past the block with ArrowDown", () => {
    expect(down("> - ```\n>   co|de\n>   ```")).toBe("> - ```\n>   code\n>   ```\n>   |");
    expect(down("- ```\n  co|de\n  ```\nafter")).toBe("- ```\n  code\n  ```\n|after");
  });

  it("never tidies inside the block", () => {
    expect(tidy("> - ```\n>   ** not bold**|\n>   ```")).toBeNull();
    expect(tidy("> - ```\n>   ** not bold**|")).toBeNull();
    expect(tidy("- ```\n  ** not bold**|")).toBeNull();
    // Past the item's end, the block is over: emphasis tidies again.
    expect(tidy("- ```\n  code\n** bold**|")).toBe("- ```\n  code\n**bold**|");
  });
});

describe("fenceEnter", () => {
  const enter = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceEnter(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action || action.kind === "newline") return action?.kind ?? null;
    return after(marked, action.edit);
  };

  it("does nothing outside a fenced block", () => {
    expect(enter("plain|")).toBeNull();
    expect(enter("```\ncode\n```\nafter|")).toBeNull();
  });

  it("makes a newline inside a block, fence lines included", () => {
    expect(enter("```\nco|de\n```")).toBe("newline");
    expect(enter("```ts|\ncode\n```")).toBe("newline");
    expect(enter("```\ncode|\n```")).toBe("newline");
    expect(enter("```\ncode\n```|")).toBe("newline");
    expect(enter("```\n|\nmore\n```")).toBe("newline");
  });

  it("exits from an empty last line, dropping it", () => {
    expect(enter("```\ncode\n|\n```")).toBe("```\ncode\n```\n|");
    expect(enter("```\n|\n```")).toBe("```\n```\n|");
    expect(enter("```\ncode\n|\n```\nafter")).toBe("```\ncode\n```\n|after");
  });

  it("closes an open block on exit", () => {
    expect(enter("```\ncode\n|")).toBe("```\ncode\n```\n|");
    expect(enter("~~~~\ncode\n|")).toBe("~~~~\ncode\n~~~~\n|");
  });

  it("leaves a selection alone", () => {
    const [text, caret] = at("```\ncode\n|\n```");
    expect(fenceEnter(text, caret - 2, caret, tokenizeComposerMarkdown(text))).toEqual({
      kind: "newline",
    });
  });
});

describe("fenceNewline", () => {
  const newline = (marked: string) => {
    const [text, caret] = at(marked);
    return after(marked, fenceNewline(text, caret, caret, tokenizeComposerMarkdown(text)));
  };

  it("keeps a quoted or listed block going", () => {
    expect(newline("> ```\n> co|de\n> ```")).toBe("> ```\n> co\n> |de\n> ```");
    expect(newline("- ```\n  co|de\n  ```")).toBe("- ```\n  co\n  |de\n  ```");
  });

  it("never exits from the empty last line", () => {
    expect(newline("> ```\n> code\n> |\n> ```")).toBe("> ```\n> code\n> \n> |\n> ```");
    expect(newline("> - ```\n>   code\n>   |")).toBe("> - ```\n>   code\n>   \n>   |");
  });

  it("leaves a bare line break to the caller elsewhere", () => {
    expect(newline("```\nco|de\n```")).toBeNull();
    expect(newline("> quoted|")).toBeNull();
    expect(newline("plain|")).toBeNull();
  });
});

describe("fenceArrowDown", () => {
  const down = (marked: string) => {
    const [text, caret] = at(marked);
    const action = fenceArrowDown(text, caret, caret, tokenizeComposerMarkdown(text));
    if (!action) return null;
    if ("move" in action) return text.slice(0, action.move) + "|" + text.slice(action.move);
    return after(marked, action.edit);
  };

  it("moves past a closed block from its last body line or closing fence", () => {
    expect(down("```\nco|de\n```\nafter")).toBe("```\ncode\n```\n|after");
    expect(down("```\ncode\n``|`\nafter")).toBe("```\ncode\n```\n|after");
  });

  it("adds a line when the block ends the draft", () => {
    expect(down("```\nco|de\n```")).toBe("```\ncode\n```\n|");
  });

  it("closes an open block first", () => {
    expect(down("```\nco|de")).toBe("```\ncode\n```\n|");
    expect(down("```\ncode\n|")).toBe("```\ncode\n```\n|");
  });

  it("moves normally elsewhere", () => {
    expect(down("```\nfi|rst\nlast\n```")).toBeNull();
    expect(down("```|\ncode\n```")).toBeNull();
    expect(down("plain|\ntext")).toBeNull();
  });
});
