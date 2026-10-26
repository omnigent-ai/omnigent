import { describe, expect, it } from "vitest";
import {
  ATTACHMENT_SIZE_LIMITS_MB,
  attachmentAccept,
  attachmentFilename,
  attachmentKey,
  classifyAttachment,
  validateAttachments,
} from "./attachments";

function makeFile(name: string, type: string, bytes = 10): File {
  return new File([new Uint8Array(bytes)], name, { type });
}

const MB = 1024 * 1024;

describe("attachmentFilename", () => {
  it.each(["screenshot.png", "notes.txt", "report.pdf"])("preserves the name %s", (name) => {
    expect(attachmentFilename(makeFile(name, ""))).toBe(name);
  });

  it("uses the upload filename for an unnamed clipboard image without renaming the File", () => {
    const file = makeFile("", "image/png");
    expect(attachmentFilename(file)).toBe("image.png");
    expect(file.name).toBe("");
  });
});

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
    expect(classifyAttachment(makeFile("data.tsv", ""))).toBe("text");
    // Windows/Excel tags .csv as application/vnd.ms-excel — extension wins.
    expect(classifyAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe("text");
  });

  it("classifies archives, Office documents and databases as files", () => {
    const pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation";
    expect(classifyAttachment(makeFile("deck.pptx", pptx))).toBe("file");
    expect(classifyAttachment(makeFile("a.zip", "application/zip"))).toBe("file");
    // Office files are zip containers, so the browser often mislabels them.
    expect(classifyAttachment(makeFile("report.docx", "application/zip"))).toBe("file");
    expect(classifyAttachment(makeFile("sheet.xlsx", "application/octet-stream"))).toBe("file");
    expect(classifyAttachment(makeFile("app.sqlite3", ""))).toBe("file");
    // The extension wins over a text MIME, matching the server.
    expect(classifyAttachment(makeFile("a.zip", "text/plain"))).toBe("file");
  });

  it("rejects types outside the supported allowlist", () => {
    expect(classifyAttachment(makeFile("a.bin", "application/octet-stream"))).toBeNull();
    expect(classifyAttachment(makeFile("a.mp4", "video/mp4"))).toBeNull();
    expect(classifyAttachment(makeFile("song.mp3", "audio/mpeg"))).toBeNull();
    expect(classifyAttachment(makeFile("noext", ""))).toBeNull();
  });

  it("recognizes a text/code extension the MIME mislabels", () => {
    // .csv tagged as an office MIME still uses the text size limit.
    expect(classifyAttachment(makeFile("data.csv", "application/vnd.ms-excel"))).toBe("text");
  });

  it("follows the server's filesystem attachment policy", () => {
    const allTypes = { extensions: [".zip"], all_types: true };
    // Inline types stay inline; anything else becomes a file.
    expect(classifyAttachment(makeFile("notes.txt", "text/plain"), allTypes)).toBe("text");
    expect(classifyAttachment(makeFile("a.png", "image/png"), allTypes)).toBe("image");
    expect(classifyAttachment(makeFile("trace.gz", "application/gzip"), allTypes)).toBe("file");
    expect(classifyAttachment(makeFile("site.har", ""), allTypes)).toBe("file");

    // A listed extension is a file even when it would otherwise be text.
    const logs = { extensions: [".log"], all_types: false };
    expect(classifyAttachment(makeFile("big.log", "text/plain"), logs)).toBe("file");
    // A list replaces the defaults.
    expect(classifyAttachment(makeFile("a.zip", "application/zip"), logs)).toBeNull();
    // An excluded default type stays refused when the browser labels it as text.
    expect(classifyAttachment(makeFile("a.zip", "text/plain"), logs)).toBeNull();
    const none = { extensions: [], all_types: false };
    expect(classifyAttachment(makeFile("a.zip", "text/plain"), none)).toBeNull();
  });
});

describe("attachmentAccept", () => {
  it("lists inline types plus the policy's filesystem extensions", () => {
    expect(attachmentAccept()).toBe(
      "image/*,application/pdf,text/*,application/json,.tsv,.zip,.docx,.xlsx,.pptx,.db,.sqlite,.sqlite3",
    );
    expect(attachmentAccept({ extensions: [".gz"], all_types: false })).toBe(
      "image/*,application/pdf,text/*,application/json,.tsv,.gz",
    );
    expect(attachmentAccept({ extensions: [".zip"], all_types: true })).toBeUndefined();
  });

  it("names .tsv, which the text/* wildcard does not cover in every browser", () => {
    // A browser that maps .tsv to no MIME greys it out in the OS picker, so
    // the filter must name it even though .csv needs no entry.
    expect(attachmentAccept()?.split(",")).toContain(".tsv");
    expect(attachmentAccept({ extensions: [".gz"], all_types: false })?.split(",")).toContain(
      ".tsv",
    );
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

  it("names the types the server accepts when rejecting", () => {
    const clip = [makeFile("clip.mp4", "video/mp4")];
    expect(validateAttachments(clip).errors[0]).toContain("archives, office documents");
    expect(
      validateAttachments(clip, { extensions: [".har", ".gz"], all_types: false }).errors[0],
    ).toContain("only images, PDF, text/code, and .gz, .har files are supported");
    expect(validateAttachments(clip, { extensions: [], all_types: false }).errors[0]).toContain(
      "only images, PDF, and text/code files are supported",
    );
  });

  it("accepts other files under the file limit when the server allows all types", () => {
    const trace = makeFile("trace.gz", "application/gzip", 30 * MB);
    expect(validateAttachments([trace]).accepted).toHaveLength(0);
    const { accepted, errors } = validateAttachments([trace], {
      extensions: [".zip"],
      all_types: true,
    });
    expect(accepted).toEqual([trace]);
    expect(errors).toHaveLength(0);
  });

  it("accepts an archive up to its larger limit", () => {
    const zip = makeFile("bundle.zip", "application/zip", 30 * MB);
    const { accepted, errors } = validateAttachments([zip]);
    expect(accepted).toEqual([zip]);
    expect(errors).toHaveLength(0);
  });

  it("rejects an archive over its limit", () => {
    const huge = makeFile("bundle.zip", "application/zip", ATTACHMENT_SIZE_LIMITS_MB.file * MB + 1);
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

  it("caps non-compressible images (SVG) at the smaller limit", () => {
    // Under 5 MB: accepted. A raster PNG the same size would be fine too, but
    // SVG can't be compressed server-side, so it keeps the small cap.
    const smallSvg = makeFile("icon.svg", "image/svg+xml", 4 * MB);
    expect(validateAttachments([smallSvg]).accepted).toHaveLength(1);

    // Between the SVG cap (5 MB) and the raster image cap (50 MB): rejected.
    const bigSvg = makeFile("big.svg", "image/svg+xml", 6 * MB);
    const { accepted, errors } = validateAttachments([bigSvg]);
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
