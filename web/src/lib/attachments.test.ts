import { describe, expect, it } from "vitest";
import {
  ATTACHMENT_SIZE_LIMITS_MB,
  attachmentKey,
  classifyAttachment,
  validateAttachments,
} from "./attachments";

function makeFile(name: string, type: string, bytes = 10): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

const MB = 1024 * 1024;

describe("attachmentKey", () => {
  it("is stable per File object and distinct for equivalent files", () => {
    const first = makeFile("a.png", "image/png");
    const second = makeFile("a.png", "image/png");

    expect(attachmentKey(first)).toBe(attachmentKey(first));
    expect(attachmentKey(first)).not.toBe(attachmentKey(second));
  });
});

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

  it("classifies archives, office documents and databases as workspace-delivered", () => {
    const pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    expect(classifyAttachment(makeFile("deck.pptx", pptx))).toBe("workspace");
    expect(classifyAttachment(makeFile("a.zip", "application/zip"))).toBe("workspace");
    // Office files are zip containers, so the browser often mislabels them.
    expect(classifyAttachment(makeFile("report.docx", "application/zip"))).toBe("workspace");
    expect(classifyAttachment(makeFile("sheet.xlsx", "application/octet-stream"))).toBe(
      "workspace",
    );
    expect(classifyAttachment(makeFile("app.sqlite3", ""))).toBe("workspace");
  });

  it("still rejects types no harness can open from disk", () => {
    expect(classifyAttachment(makeFile("a.bin", "application/octet-stream"))).toBeNull();
    expect(classifyAttachment(makeFile("a.mp4", "video/mp4"))).toBeNull();
    expect(classifyAttachment(makeFile("song.mp3", "audio/mpeg"))).toBeNull();
    expect(classifyAttachment(makeFile("noext", ""))).toBeNull();
  });

  it("keeps inline delivery for a text/code extension the MIME mislabels", () => {
    // .csv tagged as an office MIME must stay text, not become workspace.
    expect(classifyAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe("text");
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
    const { accepted, errors } = validateAttachments([makeFile("clip.mp4", "video/mp4")]);
    expect(accepted).toHaveLength(0);
    expect(errors).toHaveLength(1);
    expect(errors[0]).toContain("clip.mp4");
  });

  it("accepts a workspace-delivered file up to its larger limit", () => {
    const zip = makeFile("bundle.zip", "application/zip", 30 * MB);
    const { accepted, errors } = validateAttachments([zip]);
    expect(accepted).toEqual([zip]);
    expect(errors).toHaveLength(0);
  });

  it("rejects a workspace-delivered file over its limit", () => {
    const huge = makeFile(
      "bundle.zip",
      "application/zip",
      ATTACHMENT_SIZE_LIMITS_MB.workspace * MB + 1,
    );
    const { accepted, errors } = validateAttachments([huge]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("rejects files over their per-type size limit", () => {
    const bigImage = makeFile("huge.png", "image/png", ATTACHMENT_SIZE_LIMITS_MB.image * MB + 1);
    const { accepted, errors } = validateAttachments([bigImage]);
    expect(accepted).toHaveLength(0);
    expect(errors[0]).toContain("too large");
  });

  it("partitions a mixed batch into accepted + errors", () => {
    const ok = makeFile("a.png", "image/png");
    const zip = makeFile("a.zip", "application/zip");
    const badType = makeFile("a.mp4", "video/mp4");
    const tooBig = makeFile("big.pdf", "application/pdf", ATTACHMENT_SIZE_LIMITS_MB.pdf * MB + 1);
    const { accepted, errors } = validateAttachments([ok, zip, badType, tooBig]);
    expect(accepted).toEqual([ok, zip]);
    expect(errors).toHaveLength(2);
  });
});
