import { describe, expect, it } from "vitest";
import type { ImageContentBlock, MessageContentBlock } from "./blocks";
import { attachmentLabel, imagePreview, isTextBlock, keyedAttachments } from "./blocks";

// `input_image` blocks arrive from the composer (file_id) and from imported
// sessions (inline `image_url`, or malformed): neither may blank a transcript.

describe("imagePreview", () => {
  it("renders an uploaded attachment from its file id", () => {
    expect(imagePreview({ type: "input_image", file_id: "file_1", filename: "shot.png" })).toEqual({
      kind: "uploaded",
      fileId: "file_1",
      alt: "shot.png",
    });
  });

  it("shows a chip while an upload is still in flight", () => {
    expect(imagePreview({ type: "input_image", file_id: "pending:shot.png" })).toEqual({
      kind: "pending",
      label: "shot.png",
    });
  });

  it("renders an imported inline data URI that carries no file id", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    expect(imagePreview({ type: "input_image", image_url: src })).toEqual({
      kind: "inline",
      src,
      alt: "Attached image",
    });
  });

  it("prefers the block's filename as the inline image's alt text", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    expect(imagePreview({ type: "input_image", image_url: src, filename: "shot.png" })).toEqual({
      kind: "inline",
      src,
      alt: "shot.png",
    });
  });

  it("falls back to a placeholder for a block with neither file id nor url", () => {
    expect(imagePreview({ type: "input_image" })).toEqual({
      kind: "unavailable",
      label: "Unavailable image",
    });
  });

  it("refuses a url scheme an <img> should never be pointed at", () => {
    // Imported transcripts are foreign data; only inline image bytes
    // become an `src`.
    expect(imagePreview({ type: "input_image", image_url: "javascript:alert(1)" })).toEqual({
      kind: "unavailable",
      label: "Unavailable image",
    });
    expect(imagePreview({ type: "input_image", image_url: "data:text/html,<script>" })).toEqual({
      kind: "unavailable",
      label: "Unavailable image",
    });
  });

  it("refuses a remote url so an import never fetches attacker-chosen bytes", () => {
    // A remote `src` would be a read receipt for every viewer of the session.
    expect(
      imagePreview({ type: "input_image", image_url: "https://attacker.example/pixel.png" }),
    ).toEqual({
      kind: "unavailable",
      label: "Unavailable image",
    });
    expect(
      imagePreview({
        type: "input_image",
        image_url: "http://attacker.example/pixel.png",
        filename: "shot.png",
      }),
    ).toEqual({
      kind: "unavailable",
      label: "shot.png",
    });
  });

  // Imported blocks are copied verbatim as plain dicts, so any field may hold any
  // type; a non-string counts as absent rather than throwing during render.
  it("ignores a file id that is not a string", () => {
    const block = { type: "input_image", file_id: 12345 } as unknown as ImageContentBlock;
    expect(imagePreview(block)).toEqual({ kind: "unavailable", label: "Unavailable image" });
  });

  it("ignores an image url that is not a string", () => {
    const block = {
      type: "input_image",
      image_url: { url: "data:image/png;base64,iVBORw0KGgo=" },
    } as unknown as ImageContentBlock;
    expect(imagePreview(block)).toEqual({ kind: "unavailable", label: "Unavailable image" });
  });

  it("ignores a filename that is not a string", () => {
    const src = "data:image/png;base64,iVBORw0KGgo=";
    expect(
      imagePreview({
        type: "input_image",
        image_url: src,
        filename: 7,
      } as unknown as ImageContentBlock),
    ).toEqual({ kind: "inline", src, alt: "Attached image" });
    expect(
      imagePreview({
        type: "input_image",
        file_id: "file_1",
        filename: { name: "shot.png" },
      } as unknown as ImageContentBlock),
    ).toEqual({ kind: "uploaded", fileId: "file_1", alt: "file_1" });
  });

  it("ignores array-valued fields", () => {
    const block = {
      type: "input_image",
      file_id: ["file_1"],
      image_url: ["data:image/png;base64,iVBORw0KGgo="],
      filename: ["shot.png"],
    } as unknown as ImageContentBlock;
    expect(imagePreview(block)).toEqual({ kind: "unavailable", label: "Unavailable image" });
  });

  it("still labels a pending upload whose filename is not a string", () => {
    const block = {
      type: "input_image",
      file_id: "pending:shot.png",
      filename: 42,
    } as unknown as ImageContentBlock;
    expect(imagePreview(block)).toEqual({ kind: "pending", label: "shot.png" });
  });
});

describe("attachmentLabel", () => {
  it("prefers the filename, then the file id", () => {
    expect(attachmentLabel({ filename: "notes.pdf", file_id: "file_1" })).toBe("notes.pdf");
    expect(attachmentLabel({ file_id: "file_1" })).toBe("file_1");
    expect(attachmentLabel({})).toBe("Attachment");
  });

  // An imported transcript is copied verbatim, so either field can hold any
  // type; a non-string must never reach the chip as a React child.
  it("falls back to the generic label when a field is not a string", () => {
    const cases = [
      { filename: { name: "notes.pdf" } },
      { filename: ["notes.pdf"] },
      { file_id: 42 },
      { filename: null, file_id: 42 },
    ] as unknown as { file_id?: string; filename?: string }[];
    for (const block of cases) {
      expect(attachmentLabel(block)).toBe("Attachment");
    }
  });

  it("still uses a string file id when the filename is not a string", () => {
    const block = { filename: 42, file_id: "file_1" } as unknown as {
      file_id?: string;
      filename?: string;
    };
    expect(attachmentLabel(block)).toBe("file_1");
  });
});

describe("isTextBlock", () => {
  it("accepts a text block that really carries text", () => {
    expect(isTextBlock({ type: "input_text", text: "hi" })).toBe(true);
  });

  it("rejects other block types and a non-string text", () => {
    expect(isTextBlock({ type: "input_file", filename: "notes.pdf" })).toBe(false);
    const block = { type: "input_text", text: { parts: ["hi"] } } as unknown as MessageContentBlock;
    expect(isTextBlock(block)).toBe(false);
  });
});

describe("keyedAttachments", () => {
  it("keys an attachment by its own identity", () => {
    expect(keyedAttachments([{ id: "file_1" }], (a) => a.id)).toEqual([
      { key: "file_1", item: { id: "file_1" } },
    ]);
  });

  it("disambiguates the same file attached twice", () => {
    const keys = keyedAttachments([{ id: "file_1" }, { id: "file_1" }], (a) => a.id).map(
      (entry) => entry.key,
    );
    expect(new Set(keys).size).toBe(2);
  });

  it("keys attachments that identify themselves with nothing at all", () => {
    const keys = keyedAttachments([{}, {}], () => undefined).map((entry) => entry.key);
    expect(new Set(keys).size).toBe(2);
  });

  it("bounds the key so a megabyte data URI never becomes one", () => {
    const src = `data:image/png;base64,${"A".repeat(5000)}`;
    const [entry] = keyedAttachments([{ src }], (a) => a.src);
    expect(entry.key.length).toBeLessThanOrEqual(96);
  });

  // An imported block's fields are unvalidated, so the identity callback can
  // hand back any type; keys must stay strings and stay distinct.
  it("keys attachments whose identity is not a string", () => {
    const items = [
      { type: "input_image", file_id: 12345 },
      { type: "input_image", image_url: { url: "data:image/png;base64,iVBORw0KGgo=" } },
      { type: "input_image", filename: ["shot.png"] },
      { type: "input_image", file_id: 12345 },
    ] as unknown as ImageContentBlock[];
    const keys = keyedAttachments(items, (img) => img.file_id ?? img.image_url ?? img.filename).map(
      (entry) => entry.key,
    );
    expect(keys.every((key) => typeof key === "string")).toBe(true);
    expect(new Set(keys).size).toBe(items.length);
  });
});
