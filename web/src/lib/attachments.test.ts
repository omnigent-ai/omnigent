import { describe, expect, it } from "vitest";
import { ATTACHMENT_SIZE_LIMITS_MB, classifyAttachment, validateAttachments } from "./attachments";

function makeFile(name: string, type: string, bytes = 10): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

const MB = 1024 * 1024;

describe("classifyAttachment", () => {
  it("classifies images by MIME", () => {
    expect(classifyAttachment(makeFile("a.png", "image/png"))).toBe("image");
    expect(classifyAttachment(makeFile("a.jpg", "image/jpeg"))).toBe("image");
  });

  it("classifies PDF by MIME or extension", () => {
    expect(classifyAttachment(makeFile("a.pdf", "application/pdf"))).toBe("pdf");
    // Some browsers report an empty type for PDFs — fall back to extension.
    expect(classifyAttachment(makeFile("a.pdf", ""))).toBe("pdf");
  });

  it("classifies text/code, including code files with empty/wrong MIME", () => {
    expect(classifyAttachment(makeFile("a.txt", "text/plain"))).toBe("text");
    expect(classifyAttachment(makeFile("a.json", "application/json"))).toBe("text");
    // .ts reports video/mp2t in some browsers; extension wins.
    expect(classifyAttachment(makeFile("a.ts", "video/mp2t"))).toBe("text");
    expect(classifyAttachment(makeFile("main.rs", ""))).toBe("text");
    expect(classifyAttachment(makeFile("notebook.ipynb", ""))).toBe("text");
    // Windows/Excel tags .csv as application/vnd.ms-excel — extension wins.
    expect(classifyAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe("text");
  });

  it("rejects office/binary types", () => {
    const pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    expect(classifyAttachment(makeFile("deck.pptx", pptx))).toBeNull();
    expect(classifyAttachment(makeFile("a.zip", "application/zip"))).toBeNull();
    expect(classifyAttachment(makeFile("a.bin", "application/octet-stream"))).toBeNull();
    expect(classifyAttachment(makeFile("a.mp4", "video/mp4"))).toBeNull();
  });
});

describe("validateAttachments", () => {
  it("accepts supported files within their size limit", () => {
    const files = [makeFile("a.png", "image/png"), makeFile("a.pdf", "application/pdf")];
    const { accepted, errors } = validateAttachments(files);
    expect(accepted).toHaveLength(2);
    expect(errors).toHaveLength(0);
  });

  it("rejects unsupported types with a message", () => {
    const pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    const { accepted, errors } = validateAttachments([makeFile("deck.pptx", pptx)]);
    expect(accepted).toHaveLength(0);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("deck.pptx");
  });

  it("rejects files over their per-type size limit", () => {
    const bigImage = makeFile("huge.png", "image/png", ATTACHMENT_SIZE_LIMITS_MB.image * MB + 1);
    const { accepted, errors } = validateAttachments([bigImage]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("partitions a mixed batch into accepted + errors", () => {
    const ok = makeFile("a.png", "image/png");
    const badType = makeFile("a.zip", "application/zip");
    const tooBig = makeFile("big.pdf", "application/pdf", ATTACHMENT_SIZE_LIMITS_MB.pdf * MB + 1);
    const { accepted, errors } = validateAttachments([ok, badType, tooBig]);
    expect(accepted).toEqual([ok]);
    expect(errors).toHaveLength(2);
  });
});
