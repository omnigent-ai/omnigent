import { describe, expect, it } from "vitest";

import {
  COMPOSER_ATTACHMENT_PLACEHOLDER,
  composerPartsFromProjection,
  composerPartsToContentBlocks,
  composerPartsToProjection,
  replaceComposerText,
  type ComposerDraftPart,
} from "./composerContent";

function image(name: string): File {
  return new File([name], name, { type: "image/png" });
}

describe("ordered composer content", () => {
  it("keeps a literal object-replacement character when no attachment fills it", () => {
    expect(composerPartsFromProjection(`a${COMPOSER_ATTACHMENT_PLACEHOLDER}b`, [])).toEqual([
      { type: "text", text: `a${COMPOSER_ATTACHMENT_PLACEHOLDER}b` },
    ]);
  });

  it("edits text without moving or deleting staged attachments", () => {
    const shot = image("shot.png");
    expect(
      replaceComposerText(
        [
          { type: "text", text: "/rev" },
          { type: "attachment", file: shot },
          { type: "text", text: " tail" },
        ],
        "/review tail",
      ),
    ).toEqual([
      { type: "text", text: "/review" },
      { type: "attachment", file: shot },
      { type: "text", text: " tail" },
    ]);
  });

  it("round-trips text and attachments without losing their exact positions", () => {
    const first = image("first.png");
    const second = image("second.png");
    const parts: ComposerDraftPart[] = [
      { type: "text", text: "before " },
      { type: "attachment", file: first },
      { type: "text", text: " between " },
      { type: "attachment", file: second },
      { type: "text", text: " after" },
    ];

    const projection = composerPartsToProjection(parts);
    expect(projection).toBe(
      `before ${COMPOSER_ATTACHMENT_PLACEHOLDER} between ${COMPOSER_ATTACHMENT_PLACEHOLDER} after`,
    );
    expect(composerPartsFromProjection(projection, [first, second])).toEqual(parts);
  });

  it("serializes attachments between the text blocks surrounding them", async () => {
    const shot = image("shot.png");
    const parts: ComposerDraftPart[] = [
      { type: "text", text: "look before" },
      { type: "attachment", file: shot },
      { type: "text", text: "then after" },
    ];

    const blocks = await composerPartsToContentBlocks(parts, async (file) => ({
      type: "input_image" as const,
      file_id: `uploaded:${file.name}`,
      filename: file.name,
    }));

    expect(blocks).toEqual([
      { type: "input_text", text: "look before" },
      { type: "input_image", file_id: "uploaded:shot.png", filename: "shot.png" },
      { type: "input_text", text: "then after" },
    ]);
  });
});
