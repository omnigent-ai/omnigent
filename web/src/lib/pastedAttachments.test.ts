import { describe, expect, it } from "vitest";
import { largePasteAsFile, uniqueAttachmentName } from "./pastedAttachments";

/** A name predicate over `@`-prefixed tokens, as the composer's allocation builds it. */
function takenFrom(takenTokens: readonly string[]) {
  return (candidate: string) => takenTokens.includes(`@${candidate}`);
}

function clipboard(text: string) {
  return { getData: (type: string) => (type === "text/plain" ? text : "") };
}

describe("largePasteAsFile", () => {
  it("keeps a paste just under both thresholds inline", () => {
    expect(largePasteAsFile(clipboard("line\n".repeat(38) + "line"))).toBeNull();
    expect(largePasteAsFile(clipboard("x".repeat(3999)))).toBeNull();
  });

  it("does not count a trailing newline as a line", () => {
    expect(largePasteAsFile(clipboard("line\n".repeat(39)))).toBeNull();
  });

  it("turns a paste of 40 lines or 4000 characters into a .txt file", () => {
    const byLines = largePasteAsFile(clipboard("line\n".repeat(39) + "line"));
    expect(byLines?.name).toBe("pasted-text.txt");
    expect(byLines?.type).toBe("text/plain");
    expect(largePasteAsFile(clipboard("x".repeat(4000)))).not.toBeNull();
  });

  it("normalizes Windows line endings before counting lines", async () => {
    const file = largePasteAsFile(clipboard("line\r\n".repeat(39) + "line"));
    expect(file).not.toBeNull();
    expect(await file!.text()).toBe("line\n".repeat(39) + "line");
  });
});

describe("uniqueAttachmentName", () => {
  it("keeps a name no token uses", () => {
    expect(uniqueAttachmentName("image.png", takenFrom(["@readme.md"]))).toBe("image.png");
  });

  it("suffixes a repeat before the extension", () => {
    expect(uniqueAttachmentName("image.png", takenFrom(["@image.png"]))).toBe("image (2).png");
    expect(uniqueAttachmentName("image.png", takenFrom(["@image.png", "@image (2).png"]))).toBe(
      "image (3).png",
    );
  });

  it("suffixes an extensionless name at its end", () => {
    expect(uniqueAttachmentName("notes", takenFrom(["@notes"]))).toBe("notes (2)");
  });
});

describe("largePasteAsFile names", () => {
  it("keeps the name clear of tray names", () => {
    const file = largePasteAsFile(clipboard("x".repeat(4000)), ["pasted-text.txt"]);
    expect(file?.name).toBe("pasted-text (2).txt");
  });
});
