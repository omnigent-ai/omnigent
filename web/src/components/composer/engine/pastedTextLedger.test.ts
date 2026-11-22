import { afterEach, describe, expect, it } from "vitest";
import { insertComposerText } from "@/lib/composerText";
import { createPastedTextLedger, type PastedTextContext } from "./pastedTextLedger";
import { createFakeComposerSurface, type FakeComposerSurface } from "./testing/fakeComposerSurface";

const lines = (label: string) =>
  Array.from({ length: 50 }, (_, index) => `${label} ${index}`).join("\n");
const FIRST = lines("first");
const SECOND = lines("second");
const clipboard = (text: string) =>
  ({ getData: (format: string) => (format === "text/plain" ? text : "") }) as DataTransfer;

const surfaces: FakeComposerSurface[] = [];
afterEach(() => {
  for (const surface of surfaces.splice(0)) surface.unregister();
});

/** A ledger over one live input, whose owner reads the input's text as it is right now. */
function liveOwner(initial: string) {
  const surface = createFakeComposerSurface(initial);
  surfaces.push(surface);
  const ledger = createPastedTextLedger({
    tailInputId: "tail",
    inputIdOf: () => "tail",
    insertText: (target, text, start, end) => insertComposerText(target, text, start, end),
  });
  const owner = { files: [] as File[] };
  const context = (): PastedTextContext => ({
    files: owner.files,
    valueOf: (inputId) => (inputId === "tail" ? surface.value : undefined),
    attach: (file) => {
      owner.files = [...owner.files, file];
      return true;
    },
    removeFile: (file) => {
      owner.files = owner.files.filter((each) => each !== file);
    },
    targetOf: () => surface,
    writeInput: () => {},
    revealInput: () => {},
    locked: false,
  });
  const paste = (text: string, at: number) => {
    surface.setSelectionRange(at, at);
    ledger.offerPaste(surface, clipboard(text), context());
  };
  return { surface, ledger, owner, context, paste };
}

/** A ledger over a tail and a reply quote, each a live input the owner can hide. */
function twoInputOwner(tail: string, quote: string) {
  const inputs = { tail: createFakeComposerSurface(tail), quote: createFakeComposerSurface(quote) };
  surfaces.push(inputs.tail, inputs.quote);
  const shown = new Set<string>(["tail", "quote"]);
  const written: Record<string, string> = {};
  const ledger = createPastedTextLedger({
    tailInputId: "tail",
    inputIdOf: (target) => (target === inputs.quote ? "quote" : "tail"),
    insertText: (target, text, start, end) => insertComposerText(target, text, start, end),
  });
  const owner = { files: [] as File[] };
  const valueOf = (inputId: string) =>
    written[inputId] ??
    (inputId === "tail" || inputId === "quote" ? inputs[inputId].value : undefined);
  const context = (): PastedTextContext => ({
    files: owner.files,
    valueOf,
    attach: (file) => {
      owner.files = [...owner.files, file];
      return true;
    },
    removeFile: (file) => {
      owner.files = owner.files.filter((each) => each !== file);
    },
    targetOf: (inputId) =>
      shown.has(inputId) && (inputId === "tail" || inputId === "quote") ? inputs[inputId] : null,
    writeInput: (inputId, value) => {
      written[inputId] = value;
    },
    revealInput: () => {},
    locked: false,
  });
  const paste = (inputId: "tail" | "quote", text: string, at: number) => {
    inputs[inputId].setSelectionRange(at, at);
    ledger.offerPaste(inputs[inputId], clipboard(text), context());
    return owner.files.at(-1)!;
  };
  return { inputs, shown, ledger, owner, context, paste, valueOf };
}

describe("createPastedTextLedger", () => {
  it("keeps another input's edit counted once across a restore elsewhere", () => {
    const { inputs, ledger, context, paste, valueOf } = twoInputOwner("abcdef", "xyz");
    const quoted = paste("quote", FIRST, 1);
    const tailPaste = paste("tail", SECOND, 3);
    // The quote changes before the owner syncs, then a tail paste is restored.
    insertComposerText(inputs.quote, "QQ", 0, 0);

    expect(ledger.insertAsText(tailPaste, context())).toBe(true);
    ledger.sync(context());
    expect(ledger.insertAsText(quoted, context())).toBe(true);

    expect(valueOf("quote")).toBe(`QQx${FIRST}yz`);
  });

  it("lets other restores run once a waiting restore's chip is removed", () => {
    const { shown, ledger, owner, context, paste, valueOf } = twoInputOwner("tail", "");
    const quoted = paste("quote", FIRST, 0);
    const tailPaste = paste("tail", SECOND, 4);
    // The owner has shown both chips, so removing one is a real removal.
    ledger.sync(context());
    shown.delete("quote");
    expect(ledger.insertAsText(quoted, context())).toBe(true);

    owner.files = owner.files.filter((file) => file !== quoted);
    ledger.sync(context());
    ledger.completePendingRestore(context());

    expect(ledger.insertAsText(tailPaste, context())).toBe(true);
    expect(valueOf("tail")).toBe(`tail${SECOND}`);
    expect(valueOf("quote")).toBe("");
  });

  it("lets other restores run once a waiting restore fails", () => {
    const { inputs, shown, ledger, owner, context, paste } = twoInputOwner("tail", "");
    const quoted = paste("quote", FIRST, 0);
    const tailPaste = paste("tail", SECOND, 4);
    shown.delete("quote");
    expect(ledger.insertAsText(quoted, context())).toBe(true);

    // The input shows again but refuses the edit, so the waiting restore keeps its chip.
    shown.add("quote");
    inputs.quote.insertText = () => false;
    ledger.completePendingRestore(context());
    expect(owner.files).toContain(quoted);

    expect(ledger.insertAsText(tailPaste, context())).toBe(true);
  });

  it("refuses a second restore while one waits for its input, then finishes the first", () => {
    const { shown, ledger, owner, context, paste, valueOf } = twoInputOwner("tail", "");
    const quoted = paste("quote", FIRST, 0);
    const tailPaste = paste("tail", SECOND, 4);
    shown.delete("quote");

    expect(ledger.insertAsText(quoted, context())).toBe(true);
    expect(ledger.insertAsText(tailPaste, context())).toBe(false);
    ledger.completePendingRestore(context());

    expect(valueOf("quote")).toBe(FIRST);
    expect(owner.files).toEqual([tailPaste]);
    expect(ledger.insertAsText(tailPaste, context())).toBe(true);
    expect(owner.files).toEqual([]);
  });

  it("shifts a later paste once past a restore, when the owner reads the input live", () => {
    const { surface, ledger, owner, context, paste } = liveOwner("abcdef");
    paste(FIRST, 1);
    paste(SECOND, 5);
    const [first, second] = owner.files;

    expect(ledger.insertAsText(first!, context())).toBe(true);
    expect(ledger.insertAsText(second!, context())).toBe(true);

    expect(surface.value).toBe(`a${FIRST}bcde${SECOND}f`);
  });

  it("keeps a just-accepted paste while the owner's files catch up, then forgets a removed one", () => {
    const { ledger, owner, context, paste } = liveOwner("");
    paste(FIRST, 0);
    const [file] = owner.files;

    // The owner reconciles against the files it had before the attachment landed.
    ledger.sync({ ...context(), files: [] });
    expect(ledger.snapshot()).toEqual({ chips: [{ file, lines: 50 }], undoable: true });

    ledger.sync(context());
    expect(ledger.snapshot().undoable).toBe(true);

    // Once the owner had it, a chip it no longer has was removed or sent.
    owner.files = [];
    ledger.sync(context());
    expect(ledger.snapshot().chips).toEqual([]);
  });

  it("forgets the draft text on a reset after every paste was restored", () => {
    const { ledger, owner, context, paste } = liveOwner("draft");
    paste(FIRST, 5);
    ledger.insertAsText(owner.files[0]!, context());
    expect(ledger.snapshot().chips).toEqual([]);
    expect(ledger.holdsDraftText()).toBe(true);

    ledger.reset();

    expect(ledger.holdsDraftText()).toBe(false);
  });
});
