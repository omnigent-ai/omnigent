// Guards the @pierre/diffs patch (patches/@pierre__diffs@1.3.6.patch) at the library: a row with
// no line on either side fails its file with a DiffMissingRowError, and an onRenderError owner
// gets every render error instead of the error and its stack printed inside the file.

import {
  CodeView,
  DEFAULT_THEMES,
  FileDiff,
  type FileDiffMetadata,
  hydratePartialDiff,
  parsePatchFiles,
  preloadHighlighter,
} from "@pierre/diffs";
import { beforeAll, beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";

const PATH = "src/long.ts";
const patch = [
  `diff --git a/${PATH} b/${PATH}`,
  "index 1111111..2222222 100644",
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -294,3 +294,3 @@",
  " line 294",
  "-line 295",
  "+line 295 changed",
  " line 296",
  "",
].join("\n");

/** A patch hydrated with text shorter than it says, so its rows find no line on either side. */
function malformedDiff(): FileDiffMetadata {
  return hydratePartialDiff("clone", parsePatchFiles(patch)[0]!.files[0]!, {
    oldFile: { name: PATH, contents: "line 1\nline 2\n" },
    newFile: { name: PATH, contents: "" },
  });
}

function mountedWrapper(): HTMLElement {
  const wrapper = document.createElement("div");
  document.body.append(wrapper);
  return wrapper;
}

/** The file's rendered error, which the library draws inside its shadow root. */
const errorWrapper = (wrapper: HTMLElement) =>
  wrapper.querySelector("diffs-container")?.shadowRoot?.querySelector("[data-error-wrapper]");

let consoleError: ReturnType<typeof vi.spyOn>;

// Loaded once, so each render draws synchronously and a thrown error reaches the test.
beforeAll(async () => {
  await preloadHighlighter({
    themes: [DEFAULT_THEMES.light, DEFAULT_THEMES.dark],
    langs: ["typescript"],
  });
});
// Finished hooks run last-registered first, so each test's views are cleaned up before this.
beforeEach(({ onTestFinished: afterTest }) => {
  consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  afterTest(() => {
    consoleError.mockRestore();
    document.body.replaceChildren();
  });
});

/** A view cleaned up when the test ends, so no late render outlives the console spy. */
function cleanedUp<View extends { cleanUp: () => void }>(view: View): View {
  onTestFinished(() => view.cleanUp());
  return view;
}

describe("FileDiff render errors (patched)", () => {
  it("prints the error inside the file and logs it when nobody owns render errors", () => {
    const wrapper = mountedWrapper();
    cleanedUp(new FileDiff({ diffStyle: "unified" })).render({
      fileDiff: malformedDiff(),
      containerWrapper: wrapper,
    });

    expect(errorWrapper(wrapper)).toBeTruthy();
    expect(consoleError).toHaveBeenCalledWith(
      expect.objectContaining({ name: "DiffMissingRowError" }),
    );
  });

  it("hands a malformed row to onRenderError, saying which sides were short, and draws nothing", () => {
    const wrapper = mountedWrapper();
    const onRenderError = vi.fn();
    const fileDiff = malformedDiff();
    cleanedUp(new FileDiff({ diffStyle: "unified", onRenderError })).render({
      fileDiff,
      containerWrapper: wrapper,
    });

    expect(onRenderError).toHaveBeenCalledTimes(1);
    const [error, failedDiff] = onRenderError.mock.calls[0]!;
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({
      name: "DiffMissingRowError",
      missingRow: {
        diffStyle: "unified",
        rowType: "context",
        shortSides: ["deletions", "additions"],
      },
    });
    expect(failedDiff).toBe(fileDiff);
    expect(errorWrapper(wrapper)).toBeFalsy();
    expect(wrapper.querySelector("diffs-container")?.shadowRoot?.querySelector("pre")).toBeFalsy();
    expect(consoleError).not.toHaveBeenCalled();
  });

  it("rethrows with disableErrorHandling, even when onRenderError is set", () => {
    const onRenderError = vi.fn();
    const strict = cleanedUp(
      new FileDiff({ diffStyle: "unified", disableErrorHandling: true, onRenderError }),
    );

    expect(() =>
      strict.render({ fileDiff: malformedDiff(), containerWrapper: mountedWrapper() }),
    ).toThrow(expect.objectContaining({ name: "DiffMissingRowError" }));
    expect(onRenderError).not.toHaveBeenCalled();
  });
});

describe("CodeView render errors (patched)", () => {
  it("passes onRenderError through to the diffs it renders", async () => {
    const root = mountedWrapper();
    // jsdom can't lay out: the view gets a window's height, so it renders its first item.
    root.getBoundingClientRect = () => new DOMRect(0, 0, 800, 600);
    root.scrollTo = () => undefined;
    const onRenderError = vi.fn();
    const view = cleanedUp(new CodeView({ diffStyle: "unified", onRenderError }));
    view.setup(root);
    view.setItems([{ id: PATH, type: "diff", fileDiff: malformedDiff() }]);
    view.render(true);

    await vi.waitFor(() => expect(onRenderError).toHaveBeenCalled());
    const [error, failedDiff] = onRenderError.mock.calls[0]!;
    expect(error).toMatchObject({ name: "DiffMissingRowError" });
    expect(failedDiff).toMatchObject({ name: PATH });
    expect(consoleError).not.toHaveBeenCalled();
  });
});
