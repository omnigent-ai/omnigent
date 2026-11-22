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

describe("createPastedTextLedger", () => {
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
